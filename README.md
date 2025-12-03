# AI 分析包：回归脚本接口故障说明

## 1. 症状概述
- `python tools/verify_stageA.py`：在 `enqueue_generates` 阶段出现 `[HTTP] /v1/generate timeout`，随后 `/v1/runTests` 同样 timeout，脚本输出 "No tasks enqueued, skipping completion check"。终端日志显示 `POST /v1/generate` 被触发但服务端迟迟无响应。
- `python tools/verify_stage6.py`：能够成功获取 token 与 diagnostics，但在触发 `/v1/runTests` 或历史日志下载时抛出 `HTTPConnectionPool(host='127.0.0.1', port=4317): Read timed out`。
- `python tools/verify_stage7.py`：`/v1/apply/batch` 接口返回 `HASH_MISMATCH`，差异 diff 中显示服务器认为 `tools/verify_stage7_target.txt/_2.txt` 的内容被修改，即便重新还原也同样失败。

这说明调用链已经到达后端接口，但服务端要么拒绝（401）、要么超时、要么校验失败，与前端改动无直接关系。

## 2. 历史时间线（摘自 docs/stage9_checklist_zh.md）
- **2025-11-23**：第一次记录 StageA/Stage6 timeout、Stage7 HASH_MISMATCH，之后每次回归都延续该问题。
- **2025-11-24**：MainController/DownloadController 拆分继续推进，但回归仍因上述接口失败而被标记为"待后端修复"。
- **2025-11-25**：DownloadController 已接入 `DownloadTaskManager/DownloadCoordinator`，GUI 可以正常启动；但所有需要 `/v1/generate`、`/v1/apply/batch` 的脚本依旧失败。

## 3. 相关代码与脚本（已复制到本目录）
- `server.py`：FastAPI 主入口，挂载 `/v1/generate`、`/v1/apply/batch` 等路由。
- `generate.py` / `tests.py` / `files.py`（位于 `logic_server/services/`）：对应的服务实现，供后端排查任务调度、batch apply 校验等逻辑。
- 测试脚本：`verify_stageA.py`、`verify_stage6.py`、`verify_stage7.py`，可直接运行复现超时 / HASH_MISMATCH。

> 其余 GUI 拆分（DownloadController 等）并不会影响以上接口。GUI 仍可启动、显示界面，只有在触发后端 API 时才会卡住。

## 4. 复现方式
1. 启动服务端：`uvicorn server:app --host 127.0.0.1 --port 4317 --reload`
2. 另开终端设置环境：
   ```powershell
   $env:LOGIC_URL = "http://127.0.0.1:4317"
   $env:AUTH_TOKEN = "dev-token-123"     # 用有效 root token 替换
   $env:LOGIC_TOKEN = "dev-token-123"
   $env:LOGIC_REQUEST_TIMEOUT = "60"
   $env:LOGIC_GENERATE_COUNT = "2"
   ```
3. 依次运行：
   - `python ai_analysis_package/verify_stageA.py`
   - `python ai_analysis_package/verify_stage6.py`
   - `python ai_analysis_package/verify_stage7.py`
4. 可选：`python tools/check_server_status.py`、`python tools/verify_history.py` 用来确认其他接口仍正常。

## 5. 期望输出 vs 实际输出
| 脚本 | 期望 | 现在实际 |
| --- | --- | --- |
| verify_stageA | 2 个 generate 任务排队 + runTests 成功 | `/v1/generate` 与 `/v1/runTests` 均 timeout，任务列表为空 |
| verify_stage6 | Diagnostics + runTests 全部返回 200 | 拉取 diagnostics 成功，但 runTests timeout，日志读取失败 |
| verify_stage7 | 一个文件成功、一个故意失败（HASH mismatch） | 两个文件都 `HASH_MISMATCH`，服务器 diff 指向同一行 |

## 6. 给接手者的建议
1. 重点排查 `logic_server/services/generate.py`、`tests.py`、`files.py` 中的 `/v1/generate`、`/v1/runTests`、`/v1/apply/batch` 逻辑，包括：任务队列是否正确写入、token 校验是否错误、batch apply 的哈希对比是否读取到最新文件等。
2. 查看服务端日志（`logs/logic_api.log`）中与这些请求对应的栈信息，确认是卡在外部调用，还是直接抛出异常导致 401/timeout。
3. 修复后重新运行 `verify_stageA/verify_stage6/verify_stage7`，并把结果同步到 `docs/stage9_checklist_zh.md` 与移交流程。

> 本目录即"AI 分析包"，供其他 AI 或接手者直接查看相关代码与脚本，减少在巨大仓库中定位文件的时间。
