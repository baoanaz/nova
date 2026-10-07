# 检索质量回归

当修改 `core/zace_core/retrieval/` 或 `contextpack/` 时，
使用固定公共靶场、相同模型/维度/配置进行质量回归。
安装见 [ENVIRONMENT.txt](../../../ENVIRONMENT.txt)，
完整清单、指标和 runner 用法见 [benches/README.md](../../../benches/README.md)。

## 1. 公共输入与索引

默认回归使用 `leveldb-v1`、`helloagents-v1`、`langchain-v1`；
另外可运行公共教程仓 `hello-agents` 与 `zace` 自检。
版本以 `benches/targets.json` 为准。LangChain 质量版本是 `41d3572`，
与新 VPS 性能版本 `e75dae1f` 分开，不混用 chunk 数和耗时。

索引在本机 `.local/` 下建立或从已核对的数据根复用。
旧机器登记的 projectId/数据根不代表新机器已经有同一索引。

```bash
uv run python benches/run.py --list-targets
# 确认索引与指纹存在后：
for target in leveldb-v1 helloagents-v1 langchain-v1; do
  uv run python benches/run.py --target "$target" --data <已核对的索引根> \
    --report ".local/bench/$target.md" --max-tokens 10000
done
```

首次建立公共索引需真实供应商凭据；内存限制和成本见 [性能手册](../perf/README.md)。
内部靶场的用例、问答、报告和 manifest 保存在 `$HOME/.key/zace/benchmarks/`，
仅通过 `--targets-file` 显式加载，不进入默认公共流程。

## 2. 质量护栏

历史公共质量基线（Voyage 1024 维、maxTokens=10000；版本与原始来源见报告索引）：

| 靶场 | R@5 | R@10 | MRR | 负例 |
|---|---:|---:|---:|---:|
| leveldb | 1.000 | 1.000 | 0.721 | 1/1 |
| HelloAgents | 0.842 | 0.842 | 0.754 | 1/1 |
| LangChain | 0.895 | 0.895 | 0.784 | 1/1 |
| 三仓合计 | 0.912 | 0.912 | 0.756 | 3/3 |

相同输入和配置下比较 recall@5、recall@10、MRR、负例和证据完整度，发现下降应查明原因，并核对：

- 输入 commit、文件/chunks、模型和维度相同。
- 没有因为凭据或缓存缺失而把向量通道降级成其他通道。
- ask 在证据不足时短路，不把路径字符串命中称为答案正确率。
- 参数 R29/R30 仍冻结；现有 golden/smoke 分数不能作为擅自调参的依据。

旧设备的历史报告见 [报告索引](../../../benches/results/README.md)。
旧数字是该设备、版本和配置的记录，不是新 checkout 的验收结果。

## 3. 只重算已有候选池

若已采集候选池，排序方案比较可以离线重算，避免重复 API 请求。
采集、重算参数以对应工具的 `--help` 和质量报告为准。
不要为小幅调参反复建索引，也不要跨指纹复用候选或查询向量。

## 4. 留档

原始含路径/问答的结果留在忽略目录，公开时仅提交经审查的必要证据。
报告标明设备、UTC 日期、zace/靶场版本、配置、样本量、降级与错误。
历史内部资料已从当前默认清单移出；Git 历史与旧证据需要另行审查后再公开。
