"""Provider 目录：真实生成服务适配器。

- base.py          VideoProvider 抽象（接一个服务 = 实现 generate()）
- kling_example.py 以可灵(视频生成 API)为蓝本的接入示例骨架
- 真实可用的适配器需要 API Key，未配置时闭环默认走 MockVideoExecutor

接入三步（详见 docs/PROVIDERS.md）：
1. 继承 VideoProvider，实现 generate()
2. 注册到 hxmv/core/executor.py 的 make_executor() 工厂
3. 设环境变量 HXMV_PROVIDER=你的provider名 启用
"""
