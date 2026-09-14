"""Brain：HxMV 的持久化记忆核心（"直接用的大脑"）。

设计目标（对齐 Hermes 记忆哲学但打破 2000 字限制）：
- 持久化：记忆落盘 ~/.hxmv/brain.json，进程退出不丢，跨目标/跨项目复用
- 直接用：每次 run 开始自动把"重要 + 与当前目标相关"的记忆注入 Planner 上下文，
  无需用户手动 recall/remember（这就是"大脑"而非"档案库"）
- 容量不设死：无 2000 字小预算——存多少都行，注入时才按
  (相关性 × 重要性) 取 top-k 并受可配 token 预算约束（大模型跑 API 预算给大，
  本地小模型调小）——用的时候才花钱，存的时候不心疼
- 会遗忘：importance 随时间衰减、长期未命中的低价值条目可被清理

记忆形态（kind）：
- LESSON  经验教训：{failure, suggestion, score} —— 闭环 FAIL→修正→PASS 自动产出
- FACT    事实：模型脾气/参数经验/用户偏好 —— 手动或升华写入
- PREF    用户偏好（同 FACT，语义上给 Planner 更高权重）

检索：零第三方依赖的中文友好相似度（字符 3-gram Jaccard），
接真实语义检索（EraHerm 等）时只需替换 _score()。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field, asdict

try:                       # POSIX
    import fcntl
except ImportError:        # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:
    msvcrt = None

BRAIN_PATH = os.environ.get("HXMV_BRAIN", os.path.expanduser("~/.hxmv/brain.json"))
DEFAULT_CHAR_BUDGET = int(os.environ.get("HXMV_MEMORY_CHARS", "8000"))  # 注入预算(字符)
MAX_ENTRIES = 5000          # 软上限：大但理智
DECAY_DAYS = 60             # 超过此天数未命中的 importance 打折
DECAY_FACTOR = 0.7


class _FileLock:
    """跨进程文件锁（POSIX `fcntl` / Windows `msvcrt` 双实现，零第三方依赖）。

    为什么必须有：Brain 是**单文件**状态。Web 面板的串行 worker 只保证
    "同一进程内不打架"，挡不住另一个进程——CLI 跑一次的同时手机在面板上又跑一次，
    两边各自 read-modify-write，后写的会把先写的经验整段抹掉（静默丢数据，最难查）。

    拿不到锁时不罢工（继续跑）：宁可偶尔有一次写竞争，也不要让整个闭环卡死。
    """

    def __init__(self, path: str):
        self.lock_path = path + ".lock"
        self._fh = None

    def __enter__(self) -> "_FileLock":
        directory = os.path.dirname(self.lock_path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        self._fh = open(self.lock_path, "a+", encoding="utf-8")
        try:
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
            elif msvcrt is not None:
                # msvcrt 只能锁"从当前位置开始的一段字节"，先保证文件非空再锁第 1 字节
                if os.path.getsize(self.lock_path) == 0:
                    self._fh.write("l")
                    self._fh.flush()
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_LOCK, 1)
        except OSError:
            pass
        return self

    def __exit__(self, *exc) -> None:
        if self._fh is None:
            return
        try:
            if fcntl is not None:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            elif msvcrt is not None:
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
        except OSError:
            pass
        finally:
            self._fh.close()
            self._fh = None


def _now() -> float:
    return time.time()


def _ngrams(text: str, n: int = 3) -> set[str]:
    text = "".join(ch for ch in text.lower() if not ch.isspace())
    return {text[i:i + n] for i in range(max(0, len(text) - n + 1))}


def similarity(a: str, b: str) -> float:
    """字符 n-gram Jaccard：中文/短文本友好，零依赖。"""
    if not a or not b:
        return 0.0
    ga, gb = _ngrams(a), _ngrams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


@dataclass
class Entry:
    content: str
    kind: str = "LESSON"            # LESSON / FACT / PREF
    importance: float = 0.6
    hit_count: int = 0
    last_hit: float = 0.0
    created: float = field(default_factory=_now)
    hits_3: bool = False            # 同源经验被验证 ≥3 次（升华标记）
    meta: dict = field(default_factory=dict)  # LESSON: {failure, suggestion, score}

    def decay(self) -> None:
        """时间衰减：长期未命中的经验淡出（会遗忘，才会留出位置给新经验）。"""
        if self.last_hit <= 0:
            return
        age_days = (_now() - self.last_hit) / 86400
        if age_days > DECAY_DAYS:
            self.importance = max(0.1, self.importance * DECAY_FACTOR ** (age_days / DECAY_DAYS))


class Brain:
    def __init__(self, path: str = BRAIN_PATH):
        self.path = path
        self.entries: list[Entry] = []
        self.load()

    # ---------- 持久化 ----------
    def load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.entries = [Entry(**e) for e in data.get("entries", [])]
        except FileNotFoundError:
            self.entries = []
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            # 记忆损坏时**先留证据再清空**：直接清空等于把用户攒下的经验无声删掉，
            # 而"为什么坏了"永远查不出来（也可能是上一版写到一半被杀进程）。
            self.entries = []
            try:
                backup = f"{self.path}.corrupt-{int(time.time())}"
                os.replace(self.path, backup)
                print(f"  ⚠ 大脑文件损坏（{e}）→ 已备份为 {os.path.basename(backup)}，从空记忆继续")
            except OSError:
                pass

    def save(self) -> None:
        directory = os.path.dirname(self.path)
        if directory:
            os.makedirs(directory, exist_ok=True)
        with _FileLock(self.path):
            # 原子替换：先写同目录临时文件，再 os.replace 顶上去。
            # 直接 open(path, "w") 的写法有个致命窗口——写到一半进程被杀（Ctrl+C、
            # 面板重启、手机被系统杀）就留下半个 JSON，下次启动只能整段丢记忆。
            fd, tmp = tempfile.mkstemp(dir=directory or ".", prefix=".brain-", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump({"entries": [asdict(e) for e in self.entries]},
                              f, ensure_ascii=False, indent=1)
                    f.flush()
                    os.fsync(f.fileno())
                os.replace(tmp, self.path)
            except BaseException:
                try:
                    os.remove(tmp)
                except OSError:
                    pass
                raise

    # ---------- 写入 ----------
    def remember(self, content: str, kind: str = "FACT",
                 importance: float = 0.6, meta: dict | None = None) -> None:
        """写入一条记忆；同 content 合并并提升 importance（重复=重要）。"""
        self._decay_all()
        for e in self.entries:
            if e.content == content:
                e.hit_count += 1
                e.importance = min(1.0, e.importance + 0.05)
                if e.hit_count >= 3:
                    e.hits_3 = True
                self.save()
                return
        self.entries.append(Entry(content=content, kind=kind,
                                  importance=importance, meta=meta or {}))
        self._trim()
        self.save()

    def remember_lesson(self, failure: str, suggestion: str, score: float,
                        context: str = "") -> None:
        """闭环 PASS 后自动回写：failure 用 suggestion 修正成功。
        同源(failure+suggestion)重复出现 → importance 累积 → 更容易被注入（升华）。"""
        key = (failure, suggestion)
        for e in self.entries:
            if e.meta.get("failure") == key[0] and e.meta.get("suggestion") == key[1]:
                e.hit_count += 1
                e.importance = min(1.0, e.importance + 0.12)
                e.meta["times"] = e.meta.get("times", 1) + 1
                e.meta["score"] = max(e.meta.get("score", 0), score)
                if e.hit_count >= 3:
                    e.hits_3 = True
                e.content = self._render_lesson(failure, suggestion, e.meta)
                self.save()
                return
        meta = {"failure": failure, "suggestion": suggestion,
                "score": score, "times": 1, "context": context[:200]}
        self.entries.append(Entry(
            content=self._render_lesson(failure, suggestion, meta),
            kind="LESSON", importance=0.55, meta=meta))
        self._trim()
        self.save()

    @staticmethod
    def _render_lesson(failure: str, suggestion: str, meta: dict) -> str:
        t = meta.get("times", 1)
        return (f"[经验×{t}] 失败「{failure}」用 {suggestion} 修正成功"
                f"（验证分 {meta.get('score', 0):.2f}）")

    # ---------- 读取/注入 ----------
    def recall(self, query: str, top_k: int = 6,
               kinds: tuple[str, ...] = ()) -> list[Entry]:
        """按 (语义相似 × 0.6 + importance × 0.4) 召回 top-k。"""
        self._decay_all()
        scored = []
        for e in self.entries:
            if kinds and e.kind not in kinds:
                continue
            sim = similarity(query, e.content) if query else 0.0
            scored.append((sim * 0.6 + e.importance * 0.4, e))
        scored.sort(key=lambda x: x[0], reverse=True)
        return [e for _, e in scored[:top_k]]

    def inject(self, query: str = "", char_budget: int = DEFAULT_CHAR_BUDGET) -> str:
        """把"该用的大脑内容"渲染成提示文本块（重要+相关，预算内取前）。"""
        if not self.entries:
            return ""
        self._decay_all()
        # 高 importance 全量候选 + 与 query 相关的补足
        cands = sorted(self.entries, key=lambda e: e.importance, reverse=True)[:60]
        rel = self.recall(query, top_k=8)
        merged: list[Entry] = []
        for e in cands + rel:
            if e not in merged:
                merged.append(e)
        block, budget = [], char_budget
        for e in merged[:30]:
            line = f"- ({e.kind}{'★' if e.importance > 0.75 else ''}) {e.content}"
            if len("\n".join(block + [line])) > budget:
                break
            block.append(line)
            e.hit_count += 1      # 被注入 = 被使用，保活
            e.last_hit = _now()
        if block:
            self.save()
            return "【HxMV 记忆（自动注入，参考并延续这些经验）】\n" + "\n".join(block)
        return ""

    def _decay_all(self) -> None:
        for e in self.entries:
            e.decay()

    def _trim(self) -> None:
        if len(self.entries) <= MAX_ENTRIES:
            return
        # 超软上限：清掉 importance 最低且长期未命中的（会遗忘）
        self.entries.sort(key=lambda e: (e.importance, e.last_hit))
        del self.entries[: len(self.entries) - MAX_ENTRIES]

    @property
    def size(self) -> int:
        return len(self.entries)

    def stats(self) -> str:
        lessons = sum(1 for e in self.entries if e.kind == "LESSON")
        facts = self.size - lessons
        return f"{self.size} 条（经验 {lessons} / 事实 {facts}）"
