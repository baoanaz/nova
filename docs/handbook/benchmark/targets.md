# Benchmark 靶场

[Benchmark 方案](README.md) · [文档中心](../../README.md)

新一代 benchmark 以六种语言各一个公开仓库为靶场，每仓固定 commit，之后所有质量与性能测试都基于它们。
靶场源码放在 nova 仓库外，只读使用，遵守各自协议。

## 靶场清单

| 语言 | 仓库 | 地址 | 固定 commit | 跟踪文件数 |
|---|---|---|---|---:|
| C | FFmpeg | https://github.com/FFmpeg/FFmpeg | `447b72d90f2c0738c973e33532cfd325e2f3c9a5` | 10904 |
| C++ | muduo | https://github.com/chenshuo/muduo | `f1fc77e0c13b80e5086ff457362c8a86d1b609d4` | 418 |
| Python | fastapi | https://github.com/fastapi/fastapi | `bb7f314287bf36e04539c572a2256d94ea45f0d6` | 3183 |
| TypeScript | pi | https://github.com/earendil-works/pi（原 `badlogic/pi-mono`，已改名） | `698a2d23b86a5045dd54554354a5e0cb7e466795` | 1927 |
| Rust | cc-switch | https://github.com/farion1231/cc-switch | `797e0276ffc20e54cbdf55e3de84505d77231c91` | 1611 |
| Kotlin | gkd | https://github.com/gkd-kit/gkd | `5a3d485e6f9f85888cb17e29f91bc6f220288321` | 951 |

- 取样日期 2026-10-16，均为默认分支 HEAD 的浅克隆。
- 文件数是 `git ls-files` 的总数，不等于实际入索引的文件数（以索引记录的「解析文件」为准）。
- FFmpeg 体量最大（约 1.1 万文件），索引耗时与 embedding 用量远高于其他五个，是性能基线里最重的一项。

## 题库规模

每个仓库 5 道 `search_context` + 5 道 `ask_project`，共 60 题。题目基于对仓库源码的实际阅读，
按两个工具的描述各自设计提问方式：

- `search_context`：功能定位、调试取证、修改前找代码，期望命中具体文件或符号；
- `ask_project`：机制、模块协作、设计与实现是否一致，期望答案覆盖关键事实并引用正确文件。

## 本地目录约定

| 内容 | 位置 |
|---|---|
| 靶场源码 | `~/github/ace/bench-repos/<仓库名>` |
| 持久索引 | `~/.nova/bench-data`（不放 `/tmp`，重启不丢；`bench/stack.sh` 管理） |
