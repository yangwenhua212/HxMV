#!/data/data/com.termux/files/usr/bin/bash
# HxMV 手机端一键安装（Termux / 安卓）
#
# 用法（在 Termux 里）：
#   bash <(curl -sL https://raw.githubusercontent.com/yangwenhua212/HxMV/main/scripts/install-termux.sh)
# 或者先 clone 再：bash scripts/install-termux.sh
#
# 为什么要 Termux：HxMV 是 Python + ffmpeg 的程序，安卓上没有"自带 Python"，
# Termux 提供一个正常的 Linux 用户空间（一条命令装 python 和 ffmpeg），
# 不用 root、不用改系统，也不会动你手机里的任何数据。
set -e

echo "▶ 1/5 更新包索引并安装 python / ffmpeg / git（约 100-200MB，需要手机流量或 Wi-Fi）"
pkg update -y
pkg install -y python ffmpeg git

echo "▶ 2/5 取代码"
cd "$HOME"
if [ -d "$HOME/HxMV/.git" ]; then
  echo "  已存在，拉取最新"
  git -C "$HOME/HxMV" pull --ff-only
else
  git clone --depth 1 https://github.com/yangwenhua212/HxMV.git "$HOME/HxMV"
fi
cd "$HOME/HxMV"

echo "▶ 3/5 环境自检"
python3 - <<'PY'
import sys
print("  Python:", sys.version.split()[0])
sys.path.insert(0, ".")
from hxmv.media import probe
print("  ffmpeg/ffprobe:", "✅" if probe.has_ffmpeg() else "❌（没装上，重跑 pkg install ffmpeg）")
print("  可用视频编码器:", probe.encoder_name(), "（没有 libx264 会自动退 mpeg4，不影响跑通）")
PY

echo "▶ 4/5 跑一条本地真渲染，验证闭环能出片（约 1-2 分钟，手机会热一点）"
python3 -m hxmv --provider local --out "$HOME/storage/shared/HxMV/试跑" \
    "一只柯基在雪地里打滚" || true

echo "▶ 5/5 配真实 AI 视频 Key（智谱 CogVideoX-Flash，免费）"
echo "  先到 bigmodel.cn 注册 → 控制台 → API Keys → 新建 → 复制"
read -r -p "  把 Key 粘进来（直接回车跳过）： " KEY
if [ -n "$KEY" ]; then
  python3 -m hxmv --set-key zhipu "$KEY"
  python3 -m hxmv --key-status
  echo "  跑一条真 AI 视频："
  python3 -m hxmv --provider zhipu --project 柯基短剧 --episode 1 "第1集：柯基在雪地里打滚"
else
  echo "  跳过。随时可以：python3 -m hxmv --set-key zhipu <KEY>"
fi

cat <<'TIP'

════════════════════════════════════════════
装好了。以后这么用：

  出片（当前目录就是项目）：
      cd ~/HxMV && python3 -m hxmv --provider zhipu --project 柯基短剧 --episode 2 "第2集：..."

  手机里起控制台（用手机浏览器开 http://127.0.0.1:8668 ）：
      cd ~/HxMV && python3 -m hxmv.server --port 8668

  产物在哪：~/storage/shared/HxMV/（相册能直接看到）

保住后台别被系统杀（重要）：
      termux-wake-lock                 # 锁住唤醒，切后台也不停
      设置 → 应用 → Termux → 省电策略 → 无限制
      （开发时再开"通知"里的 Termux 前台服务）
════════════════════════════════════════════
TIP
