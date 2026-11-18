# NOC Agent Assistant

AI Agent Execution Engine cho vận hành mạng viễn thông. Project xây dựng một trợ lý NOC có thể hiểu câu hỏi tự nhiên, gọi tool nghiệp vụ, stream quá trình xử lý theo thời gian thực, lưu lịch sử hội thoại, ghi trace Langfuse và chặn các thao tác remediation nguy hiểm bằng human approval.

## Mục tiêu

NOC Agent Assistant không được thiết kế như một chatbot trả lời tự do. Hệ thống hướng tới một agent vận hành có kiểm soát:

- Trả lời dựa trên dữ liệu thật từ alarm, inventory và node health.
- Chuẩn hóa nhiều LLM provider qua adapter để AgentEngine không phụ thuộc trực tiếp SDK cụ thể.
- Stream realtime các bước xử lý qua SSE để người dùng thấy agent đang làm gì.
- Lưu conversation, tool calls và thinking content để reload không mất ngữ cảnh.
- Ghi observability bằng Langfuse: trace, LLM generation, tool span, latency, token usage và provider/tool error.
- Chặn dangerous remediation bằng human-in-the-loop approval.
- Có evaluation framework để kiểm workflow, tool routing, approval safety và answer quality.

## Tính năng chính

### Chat với AI Agent

- Frontend React/Vite cung cấp giao diện chat.
- Người dùng chọn provider: OpenAI, Anthropic hoặc DeepSeek/OpenRouter.
- Backend stream event qua `text/event-stream`.
- UI hiển thị text, thinking block, tool card, tool input/output và approval card.

### Agent Execution Loop

AgentEngine chạy vòng lặp ReAct-style:

1. Nhận user message và history.
2. Gọi provider streaming API với tool schema.
3. Nhận text delta, thinking content hoặc tool calls.
4. Emit event về frontend.
5. ToolExecutor chạy skill tương ứng.
6. Append tool result vào history.
7. Lặp lại cho tới khi có final answer hoặc đạt giới hạn iteration.

Giới hạn hiện tại: tối đa `10` iterations cho một request.

### NOC Skills

| Skill | Mục đích | Nguồn dữ liệu |
| --- | --- | --- |
| `noc_alarm_enrichment` | Tra alarm theo alarm type/window, trích xuất entity từ log và enrich với inventory. | ClickHouse, PostgreSQL |
| `alarm_statistics` | Thống kê alarm theo severity, error code hoặc device. | ClickHouse, PostgreSQL |
| `device_inventory_lookup` | Tra inventory theo IP, device name hoặc device ID. | PostgreSQL, ClickHouse |
| `node_health_remediate` | SSH vào node, đo CPU/RAM/disk/load và đề xuất remediation. | SSH node |

### Human Approval Guardrail

Với `node_health_remediate`, các action nguy hiểm như `restart_docker` và `restart_service` không được chạy ngay khi `dry_run=false`.

Luồng approval:

1. Skill trả `needs_approval=true`.
2. Backend lưu pending approval gồm host, service, action, command, metrics, thresholds và reason.
3. Frontend hiển thị card `Approve / Reject`.
4. Nếu reject: backend lưu `approval_status=rejected`, `executed=false`.
5. Nếu approve: backend chỉ chạy đúng command đã lưu trong pending approval.

Điểm quan trọng: approve path không gọi lại skill để đo lại metric hay quyết định command mới. Điều này tránh race condition: người dùng approve một command nhưng hệ thống lại tự đổi sang command khác.

### Observability

Langfuse được dùng cho observability/health:

- Một lượt chat là một trace `agent-run`.
- Mỗi LLM call là một generation.
- Mỗi tool call là một span.
- Metadata gồm provider/model, latency, token usage, tool arguments, tool result/error và tổng số tool calls.
- Thinking content không gửi full mặc định; hệ thống chỉ ghi `thinking_present` và `thinking_chars`. Chỉ khi bật `LANGFUSE_TRACE_THINKING_CONTENT=true` mới gửi full thinking vào metadata.

### Evaluation

Framework đánh giá có hai nhóm:

- Deterministic evaluation: hard check cho backend logic, skill/tool contract, agent workflow, approval và safety.
- Ragas Quality Evaluation: optional quality signal cho groundedness/correctness dựa trên frozen tool observations và human-labeled reference.

Kết quả gần nhất:

- 365 backend tests OK (34 skip khi thiếu PostgreSQL/Docker/SSH thật), lần chạy ngày 18/11/2026.
- 12 Contract Evals, 16 Adversarial Evals và 20 End-to-End Workflow Evals passed (offline).
- 11 Ragas frozen real snapshots executed successfully.

## Kiến trúc tổng quan

```text
Frontend React/Vite
  - Chat UI
  - Provider selector
  - Tool cards
  - Approval controls
  - Conversation sidebar

FastAPI Backend
  - /api/chat SSE
  - /api/chat/approve SSE
  - /api/conversations
  - Agent runtime bootstrap

Agent Runtime
  - AgentEngine
  - Provider adapters
  - ToolExecutor
  - SkillRegistry
  - EventEmitter

Data & Observability
  - Conversation DB
  - Pending approvals
  - ClickHouse alarm data
  - PostgreSQL inventory data
  - SSH node
  - Langfuse trace
```

## Cấu trúc thư mục

```text
.
├── backend/
│   ├── controllers/          # FastAPI routes cho chat, approval, history
│   ├── core/                 # Schemas, config, event emitter
│   ├── evals/                # Contract evals, workflow evals, Ragas datasets/reports
│   ├── repositories/         # Conversation DB, approval repo, ClickHouse/Postgres repo
│   ├── scripts/              # PowerShell scripts chạy eval/Ragas/snapshot
│   ├── services/
│   │   ├── providers/        # OpenAI, Anthropic, DeepSeek/OpenRouter adapters
│   │   └── skills/           # NOC skills
│   ├── tests/                # Unit/integration tests
│   ├── main.py               # FastAPI app startup
│   └── docker-compose.yml
├── frontend/
│   ├── src/
│   │   ├── components/       # Chat UI components
│   │   ├── hooks/useChat.ts  # SSE parser + chat state
│   │   ├── lib/api.ts        # API client
│   │   └── types/
│   └── package.json
└── docs/
    ├── diagrams/             # Diagram images
    └── presentation/         # Report, slide outline, demo script
```

## Yêu cầu môi trường

- Docker Desktop / Docker Compose.
- Node.js + npm nếu chạy frontend trực tiếp.
- API key hoặc OpenRouter key nếu dùng provider thật.
- Langfuse keys nếu muốn ghi trace.
- SSH credentials nếu demo node health/remediation thật.
- ClickHouse/PostgreSQL data source nếu chạy full data integration.

## Cấu hình môi trường

Backend đọc config từ environment variables. Khi chạy bằng Docker Compose, file `backend/.env.production` được nạp qua `env_file`.

Các biến quan trọng:

| Biến | Ý nghĩa |
| --- | --- |
| `OPENAI_API_KEY` | API key cho OpenAI/OpenRouter-compatible provider. |
| `OPENAI_MODEL` | Model dùng cho provider OpenAI. |
| `OPENAI_BASE_URL` | Base URL OpenAI-compatible. |
| `ANTHROPIC_API_KEY` | API key cho Anthropic/OpenRouter-compatible provider. |
| `ANTHROPIC_MODEL` | Model Anthropic. |
| `ANTHROPIC_BASE_URL` | Nếu có giá trị, Anthropic adapter chạy qua OpenAI-compatible API. |
| `DEEPSEEK_API_KEY` | API key cho DeepSeek/OpenRouter-compatible provider. |
| `DEEPSEEK_MODEL` | Model DeepSeek. |
| `DEEPSEEK_BASE_URL` | Base URL DeepSeek/OpenAI-compatible. |
| `DATABASE_URL` | Conversation DB URL. Mặc định là SQLite async. |
| `LANGFUSE_PUBLIC_KEY` | Langfuse public key. |
| `LANGFUSE_SECRET_KEY` | Langfuse secret key. |
| `LANGFUSE_HOST` | Langfuse host, mặc định cloud Langfuse. |
| `LANGFUSE_TRACE_THINKING_CONTENT` | Có gửi full thinking content lên Langfuse hay không. Mặc định false. |
| `SSH_HOST`, `SSH_USER`, `SSH_PASSWORD`, `SSH_KEY_PATH`, `SSH_PORT` | Cấu hình SSH node. |
| `CLICKHOUSE_HOST`, `CLICKHOUSE_PORT`, `CLICKHOUSE_USER`, `CLICKHOUSE_PASSWORD`, `CLICKHOUSE_DATABASE` | Kết nối ClickHouse alarm data. |
| `POSTGRES_HOST`, `POSTGRES_PORT`, `POSTGRES_USER`, `POSTGRES_PASSWORD`, `POSTGRES_DB` | Kết nối PostgreSQL inventory data. |
| `CORS_ORIGINS` | Danh sách origin frontend được phép gọi backend. |

Không commit secret thật vào repository.

## Chạy local

Cách nhanh nhất là dùng `make` (xem `make help`):

```bash
make up            # Compose: PostgreSQL + migrate + backend + worker ngoài + frontend, http://localhost:8080
make check         # ruff + unit test + eval offline, giống job backend của CI
make check-frontend
```

Stack này dùng dữ liệu mẫu (`DATA_MODE=fixture`), không cần API key. Kiến trúc hiện tại:
[operations/architecture.txt](operations/architecture.txt); vận hành: [operations/runbook.txt](operations/runbook.txt).
Các mục bên dưới là cách chạy thủ công cũ và có thể lỗi thời.

### 1. Chạy backend

```bash
cd backend
docker compose up --build
```

Backend chạy tại:

```text
http://localhost:8000
```

Health check:

```bash
curl http://localhost:8000/health
```

### 2. Chạy frontend

```bash
cd frontend
npm install
npm run dev
```

Frontend chạy tại:

```text
http://localhost:5173
```

### 3. Build frontend

```bash
cd frontend
npm run build
```

Hiện build có thể cảnh báo bundle chunk lớn hơn 500 KB. Đây là warning của Vite/Rollup, không phải lỗi build.

## API chính

### Health

```http
GET /health
```

Trả về trạng thái backend.

### Config providers

```http
GET /api/config
```

Trả danh sách provider frontend có thể hiển thị.

### Chat stream

```http
POST /api/chat
Content-Type: application/json
```

Request:

```json
{
  "message": "Tra giúp tôi thông tin inventory của IP 10.10.8.86",
  "provider": "openai",
  "conversation_id": null
}
```

Response là SSE `text/event-stream`.

### Approval decision

```http
POST /api/chat/approve
Content-Type: application/json
```

Request:

```json
{
  "conversation_id": "conversation-id",
  "call_id": "tool-call-id",
  "approved": false
}
```

Response cũng là SSE stream, gồm tool result, text summary và final answer.

### Conversation history

```http
GET /api/conversations
GET /api/conversations/{conversation_id}/messages
PUT /api/conversations/{conversation_id}/title
DELETE /api/conversations/{conversation_id}
```

## SSE event contract

Backend emit các event sau:

| Event | Ý nghĩa |
| --- | --- |
| `thinking` | Nội dung tiến trình xử lý/reasoning summary được phép hiển thị. |
| `text_delta` | Token/text final answer stream từng phần. |
| `tool_call_start` | Agent bắt đầu gọi tool, gồm tool name và arguments. |
| `tool_call_result` | Tool trả kết quả hoặc lỗi. |
| `approval_required` | Tool cần human approval trước khi execute. |
| `final_answer` | Agent kết thúc lượt xử lý. |
| `conversation_id` | Backend trả conversation id để frontend tiếp tục thread. |
| `error` | Lỗi runtime/provider/tool stream. |

Frontend parse stream trong `frontend/src/hooks/useChat.ts`.

## Demo nhanh

### Inventory lookup

```text
Tra giúp tôi thông tin inventory của IP 10.10.8.86
```

Kỳ vọng:

- Agent gọi `device_inventory_lookup`.
- UI hiển thị tool card.
- Final answer dựa trên tool result, không đoán nếu không tìm thấy dữ liệu.

### Alarm enrichment

```text
Kiểm tra giúp tôi cảnh báo Signal Loss trong 60 phút qua
```

Kỳ vọng:

- Agent gọi `noc_alarm_enrichment`.
- Tool input có alarm type và window.
- Final answer tóm tắt alarm/severity/device nếu có dữ liệu.

### Node health dry-run

```text
Kiểm tra health node anhvhn.duckdns.org, chỉ dry-run
```

Kỳ vọng:

- Agent gọi `node_health_remediate`.
- Tool trả CPU/RAM/disk/load.
- Nếu có action đề xuất thì vẫn `executed=false`.

### Human approval reject path

Dùng case này để demo guardrail an toàn:

```text
Dùng tool node_health_remediate kiểm tra node anhvhn.duckdns.org và restart docker với dry_run=false, đặt ram_threshold=0 và cpu_threshold=0.
```

Kỳ vọng:

- Agent gọi `node_health_remediate`.
- Skill sinh action `restart_docker`.
- Vì `dry_run=false` và action nguy hiểm, backend emit `approval_required`.
- UI hiện card `Approve / Reject`.
- Chọn `Reject`.
- Kết quả trả về `executed=false`, command không được thực thi.

Không dùng provider `mock` cho demo này, vì mock provider hiện cố định kịch bản tool call. Hãy chọn OpenAI, Anthropic hoặc DeepSeek.

## Chạy tests và evals

### Unit/Integration Tests

```bash
cd backend
docker compose run --rm -v D:/Code/VDT/Agent/backend:/app backend python -m unittest discover -s tests
```

Trên PowerShell có thể dùng script:

```powershell
cd backend
.\scripts\run_eval_suite.ps1
```

Script này chạy:

1. Unit/Integration Tests.
2. Contract/Integration Evals.
3. End-to-End Workflow Evals.

### Contract/Integration Evals

```bash
cd backend
docker compose run --rm -v D:/Code/VDT/Agent/backend:/app backend python -m evals.runner --kind contracts
```

### End-to-End Workflow Evals

```bash
cd backend
docker compose run --rm -v D:/Code/VDT/Agent/backend:/app backend python -m evals.runner --kind benchmark
```

Report được ghi vào:

```text
backend/evals/results/report.json
backend/evals/results/report.csv
```

### Ragas Quality Evaluation

Ragas là optional dependency. Cài theo `backend/requirements-eval.txt` hoặc dùng volume dependency riêng theo script.

```powershell
cd backend
.\scripts\run_ragas_eval.ps1
```

Ragas đọc dataset frozen snapshots:

```text
backend/evals/datasets/noc_agent_ragas_quality_cases.json
```

Report quality được ghi vào:

```text
backend/evals/results/ragas_quality/report.json
backend/evals/results/ragas_quality/report.csv
```

## Ghi chú về safety

- `dry_run=true` là mặc định an toàn cho remediation.
- Dangerous actions không execute ngay nếu `dry_run=false`.
- `restart_docker` và `restart_service` yêu cầu human approval.
- Service/container name được quote bằng `shlex.quote` trước khi ghép command để giảm rủi ro command injection.
- Approve path chạy command đã lưu trong pending approval, không gọi lại skill để tự quyết định command mới.

## Troubleshooting

### Docker không kết nối được daemon

Nếu gặp lỗi quyền trên Windows:

```text
open //./pipe/docker_engine: Access is denied
```

Hãy mở Docker Desktop và chạy terminal với quyền phù hợp.

### Frontend build lỗi Rollup optional dependency

Nếu dùng `node_modules` được cài từ môi trường khác, Rollup/Vite có thể thiếu optional native package. Cách xử lý:

```bash
cd frontend
rm -rf node_modules
npm install
npm run build
```

Trên Windows, có thể xóa `node_modules` bằng File Explorer hoặc PowerShell rồi chạy lại `npm install`.

### Provider trả rate limit/API key error

Backend sẽ cố gắng format lỗi provider thành thông báo dễ hiểu. Kiểm tra:

- API key.
- Base URL.
- Model name.
- Quota/rate limit của provider.

### Không thấy approval card

Kiểm tra:

- Prompt có `dry_run=false` chưa.
- Action có thuộc dangerous actions không (`restart_docker`, `restart_service`).
- Provider có gọi đúng `node_health_remediate` không.
- Nếu demo restart docker ổn định, dùng threshold thấp như `ram_threshold=0` và `cpu_threshold=0`.

## Trạng thái hiện tại

Kết quả lần chạy gần nhất (18/11/2026, máy local); số liệu cập nhật được ghi trong `upgrade.md`:

- Backend: 365 test OK, 34 skip khi thiếu PostgreSQL/Docker/SSH thật; Ruff pass.
- Eval offline: 12 contract, 16 adversarial, 20 workflow passed.
- Frontend: eslint, typecheck, 9 unit test, 13 Playwright E2E (fixture) pass.
- Ragas quality snapshots: 11 executed successfully trong lần chạy gần nhất được ghi nhận (chưa chạy lại).
- Chưa kiểm chứng: GitHub CI (chưa có PR nên chưa chạy), provider/ClickHouse/SSH thật, holdout NOC.

## Hướng phát triển

- Nguồn KPI/link/topology/maintenance live và dữ liệu NOC có nhãn để hiệu chuẩn các skill triage (xem `operations/skill-packages.txt`).
- SSO/JWKS thay cho bearer principal cấu hình sẵn; lọc dòng theo site cho alarm/inventory tổng hợp.
- `@resource` và chọn model trên UI; chèn intervention giữa lúc một batch tool đang chạy.
- Mở rộng Ragas dataset định kỳ, có hold-out set để tránh overfit prompt/model vào một bộ snapshot cố định.
- Cân nhắc LangGraph hoặc workflow engine nếu agent flow phát triển thành nhiều branch/state phức tạp.
