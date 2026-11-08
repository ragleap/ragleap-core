> 本文件是 README 的简体中文翻译，为 AI 辅助（AI-assisted）起草的草稿，尚未经过中文母语者审阅。如与英文 README 有出入，请以英文为准：[README.md](README.md)。

# ragleap-physical

**状态：pre-alpha（极早期阶段）。仅限模拟。这里的任何内容都不会控制真实硬件。**

一个小型库，把 RagLeap 的 AI 员工连接到开源模拟器，让人们可以安全地构建和测试。观察优先。每一个动作都要经过提案、批准，以及一个位于桥接层中、集中在一处的安全包络（safety envelope）。模型无法更改这些限值。

## 目前能用的（以及不能用的）

- 观察与提案格式（v0）、四个能力等级、安全包络、绑定到确切那一条提案的批准、试运行、结果检查，以及无需批准的停止。已用一个内置的极小单关节模拟器测试（仅用标准库）。
- 给 AI 智能体的工具恰好四个：列出数据源、获取观察、获取安全包络、提出动作提案。它们都不能批准、执行、停止或更改任何限值。一个小型 MCP 服务器（标准库实现，默认仅限本机回环地址，可选 bearer 令牌）提供这些工具。已针对官方 MCP Python SDK（`mcp>=1.28,<2`）测试。RagLeap 自己的 MCP 客户端没有被阅读或测试过：它可能需要修改（例如 HTTPS）。
- 作为可选扩展提供的 MuJoCo 适配器：一个仅观察（observe-only）的适配器，以及一个在模拟中跟随 `set_position_target` 的伺服适配器，且只能通过桥接层使用。
- 工作空间限值：在 x、y、z 上的一个长方体区域，每一个被跟踪的点都必须保持在其中；会在初始状态和试运行时检查，并在实际执行的运动中按短时间片再次检查。如果设置了工作空间而适配器无法报告位置，则一律拒绝。一个双连杆机械臂模型（内置测试替身和 MuJoCo 两种）用来检验这一功能。
- 一台模拟的机床（刀具磨损）：AI 只能调整刀具偏置（单位为微米），且必须在一个安全包络之内（范围、步长、两次更改之间的最小间隔）。更换刀具是人工操作，不是指令。这只是一个使用虚构数字的模型，对真实机床不说明任何问题。
- 经另外两个 AI 会话独立审查后加固（见 CHANGELOG）：更严格的输入与预测检查，禁止自我批准。
- 一个三关节机械臂模型（`examples/robots/` 中的 MuJoCo XML，外加一个内置测试替身）、一个无需安装 ROS 的离线 ROS 2 仅观察（observe-only）模拟实现，以及 `benchmarks/` 中的一个可重复性实验（在同一个已安装环境内可精确重放；我们不声称跨机器的可重复性）。
- 一个操作员控制台（`ragleap_physical.console`）：桥接层的人工一侧。一位具名人员可以查看待处理的提案，批准确切的摘要（digest），执行，并且可以停止。仅限本机回环地址，每人一个令牌，批准人的姓名取自令牌，绝不取自请求。它不是工具：AI 无法访问它。
- 两个仅观察（observe-only）的工业模拟实现，不使用网络，也没有真实的电表或机器：一个模拟的能源站点，以及一个带严格限值的 MTConnect 文档读取器（它拒绝 DTD 和实体，并限制文件大小）。这些适配器是仅观察的：它们仅读取你提供给它们的文档，不发送任何内容。参见 `docs/industry/observe-only-adapters.md` 以及第一小时上手指南 `docs/first-hour.md`。
- 尚未构建：第二个模拟器、真实硬件、Java 和 npm 客户端。
- 模拟不等于现实。不要把通过的模拟测试当作对真实机器的证明。

## 始终不在范围之内

生命安全系统、医疗器械、在公共道路上行驶的车辆、武器，以及任何在没有独立硬件安全停止装置的情况下可能伤害人员的事物。

## 文档

- 计划、决策、安全规则与约定：[docs/HANDOVER.md](docs/HANDOVER.md)
- 设计（格式、安全包络、威胁模型）：[docs/design/phase0-interface.md](docs/design/phase0-interface.md)

## 试一试

```
pip install -e ".[test]"          # core only, standard library
pip install -e ".[mujoco,test]"   # with the optional MuJoCo adapter
pip install -e ".[test,interop]"  # also runs the MCP SDK interop test
pytest
python examples/demo_fake.py
python examples/two_link_workspace_demo.py
```

请通过仓库的 Security 标签页私下报告漏洞（见 `SECURITY.md`）。

## 其他语言（草稿，未经母语者审阅）
[印地语](README.hi.md) · [泰米尔语](README.ta.md)
