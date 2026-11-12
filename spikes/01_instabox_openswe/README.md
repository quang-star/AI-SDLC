# Spike 01: InstaBox + Deep Agents

## Setup local nhanh (Windows)

9Router local dùng endpoint `http://127.0.0.1:20128/v1`. Mở 9Router trước, rồi
trong terminal tại workspace chạy:

```powershell
. .\spikes\01_instabox_openswe\setup_local.ps1
```

Script hỏi API key bằng ô nhập ẩn nếu `NINE_ROUTER_API_KEY` chưa có, kiểm tra
model catalog và nạp mapping vai trò vào environment của terminal hiện tại.
Không ghi key ra file, không gọi inference và không tạo VM. Catalog có thể được
server cho đọc không cần xác thực, nên đọc được catalog chưa chứng minh key có
quyền gọi model.

Mapping local mặc định nằm trong [models.local.example.json](models.local.example.json),
dùng các ID đã thấy trong `/v1/models` trên máy lúc setup; chưa xác nhận provider
có quota hay tool calling hoạt động. Bạn có thể sửa mapping hoặc truyền
`-ModelsPath <file-json-mapping>` cho script. Không đặt credential trong mapping.

Sau khi nạp thêm `INSTABOX_API_KEY` trong cùng terminal, thử cấu hình trước:

```powershell
.\spikes\01_instabox_openswe\.venv\Scripts\python.exe spikes/01_instabox_openswe/run_live_vm_spike.py --with-agent --roles dev reviewer --preflight-only --skip-api-preflight
```

Lệnh này chỉ validate local. Bỏ `--preflight-only --skip-api-preflight` mới chạy
VM smoke thật. Kết thúc terminal sẽ mất các biến môi trường vừa thiết lập.

## Chạy qua 9Router

9Router là đường gọi mặc định cho model ID không có provider prefix. Runner hỗ trợ
`<model-id>` hoặc `9router:<model-id>` và dùng Chat Completions với tool calling. Lấy model ID
chính xác từ dashboard hoặc `/v1/models` của 9Router; không bỏ prefix của router
như `cc/`, `cx/` hoặc tên combo bạn đã đặt.

Nguồn: [9Router quick start](https://github.com/decolua/9router/blob/master/gitbook/content/en/getting-started/quick-start.md),
[tích hợp OpenAI-compatible](https://github.com/decolua/9router/blob/master/gitbook/content/en/integration/other-tools.md).

Cài package trong virtualenv của spike:

```powershell
.\spikes\01_instabox_openswe\.venv\Scripts\python.exe -m pip install -r spikes/01_instabox_openswe/requirements.txt
```

Trong shell đã nạp `INSTABOX_API_KEY` và `NINE_ROUTER_API_KEY` (key của router),
đặt URL và model:

```powershell
$env:NINE_ROUTER_BASE_URL = 'http://localhost:20128/v1'
$env:NINE_ROUTER_MODEL = '<model-id-trong-dashboard>'

# Chỉ kiểm tra cấu hình local, không gọi API hay tạo VM:
.\spikes\01_instabox_openswe\.venv\Scripts\python.exe spikes/01_instabox_openswe/run_live_vm_spike.py --with-agent --preflight-only --skip-api-preflight

# Kiểm tra cấu hình và xác thực InstaBox, không tạo VM:
.\spikes\01_instabox_openswe\.venv\Scripts\python.exe spikes/01_instabox_openswe/run_live_vm_spike.py --with-agent --preflight-only

# Tạo VM, chạy sandbox smoke + agent ghi file, rồi xóa VM:
.\spikes\01_instabox_openswe\.venv\Scripts\python.exe spikes/01_instabox_openswe/run_live_vm_spike.py --with-agent
```

- `--model '<model-id>'` ưu tiên hơn `NINE_ROUTER_MODEL`, rồi `DEEPAGENTS_MODEL`
  (tương thích cấu hình cũ). Không có model hardcode: cần chọn route có thật trong router.
- URL ưu tiên `NINE_ROUTER_BASE_URL`, rồi `OPENAI_BASE_URL`, rồi mặc định
  `http://localhost:20128/v1`.
- Key ưu tiên `NINE_ROUTER_API_KEY`, rồi `OPENAI_API_KEY`. Không cần key của
  Anthropic/Google/OpenAI upstream khi gọi qua router.
- Runner không tự đọc `.env`; credentials lấy từ environment của tiến trình.
- LLM client chạy trên máy chạy Python; `localhost` ở đây là máy đó. Nếu chạy
  Python trong container/máy khác, dùng địa chỉ 9Router mà máy đó truy cập được.
- Model/route cần hỗ trợ tool calling để Deep Agents thao tác sandbox.
- Preflight kiểm tra cấu hình LLM local và API InstaBox; chưa gọi model để
  xác thực router key, model ID hay quyền tool calling. Bước agent smoke mới
  xác minh tích hợp thật.

## Nhiều model và model theo vai trò

Chạy nhiều model lần lượt trên cùng VM:

```powershell
.\spikes\01_instabox_openswe\.venv\Scripts\python.exe spikes/01_instabox_openswe/run_live_vm_spike.py --with-agent --models '<model-id-A>' '<model-id-B>' '<model-id-C>'
```

Hoặc dùng file mẫu [models.example.json](models.example.json), thay các ID bằng
model/combo đã cấu hình trong dashboard 9Router, rồi nạp mapping vào environment:

```powershell
$env:NINE_ROUTER_ROLE_MODELS = Get-Content spikes/01_instabox_openswe/models.example.json -Raw

# Chỉ kiểm tra Dev và Reviewer, theo đúng thứ tự đã chọn:
.\spikes\01_instabox_openswe\.venv\Scripts\python.exe spikes/01_instabox_openswe/run_live_vm_spike.py --with-agent --roles dev reviewer

# Chạy toàn bộ vai trò trong mapping (khi không có NINE_ROUTER_MODEL/DEEPAGENTS_MODEL):
.\spikes\01_instabox_openswe\.venv\Scripts\python.exe spikes/01_instabox_openswe/run_live_vm_spike.py --with-agent
```

`--model`, `--models`, `--roles` loại trừ nhau. `--roles` ưu tiên hơn biến
single-model; không cần xóa biến đó khi chọn vai trò cụ thể. Nếu không có cờ chọn,
ưu tiên single-model environment, rồi toàn bộ role mapping. Role mapping có thể
thêm `architect`, `planner` hoặc tên vai trò lowercase khác.

Mỗi model/vai trò chạy một agent độc lập, có file kiểm tra riêng và dòng kết quả
PASS/FAIL. Nếu một model lỗi, runner tiếp tục thử các model còn lại rồi trả mã lỗi;
VM mới tạo được dọn một lần sau cùng. Các lượt chạy tuần tự để tránh các agent
cùng ghi vào workspace. Đây là kiểm tra kết nối/tool calling theo model, chưa phải
workflow BA -> Dev -> Tester. Module `model_routing.py` chứa logic chọn model để
các adapter SDLC sau này dùng chung. Cấu hình endpoint/key dùng chung cho tất cả
route 9Router. Provider rõ ràng như `anthropic:...` vẫn gọi trực tiếp upstream;
để mọi model qua router, dùng bare ID hoặc prefix `9router:`.

Không tự đổi sang model khác khi một bước SDLC lỗi; danh sách trên là các lượt
smoke test độc lập. Combo/fallback do bạn cấu hình trong 9Router có thể dùng như
một model ID bình thường.

## Kiểm tra offline

```powershell
.\spikes\01_instabox_openswe\.venv\Scripts\python.exe -m pytest spikes/01_instabox_openswe -q
```

Test 9Router dùng HTTP transport giả lập, không gọi model hoặc InstaBox thật.
Phase 1 kiểm tra sandbox và agent ghi file (`LIVE PASS` với Dev/Reviewer theo log người dùng);
harness Phase 2 đạt `OFFLINE PASS` (139 tests toàn spike) và hiện `LIVE NOT RUN`.

## Phase 2: Bugfix trên repo thử nghiệm & Verify độc lập (`OFFLINE PASS` / `LIVE NOT RUN`)

Phạm vi Phase 2 chỉ kiểm chứng luồng Deep Agent sửa bug + `pre_agent_snapshot_id` + Hard Gates + chạy `pytest` độc lập ngoài agent trên `spikes/01_instabox_openswe/fixtures/mini_bug_repo` (chưa phải hệ thống AI SDLC hoàn chỉnh).

1. Nạp biến môi trường 9Router local và InstaBox:

```powershell
. .\spikes\01_instabox_openswe\setup_local.ps1
$env:INSTABOX_API_KEY = "<instabox-api-key>"
```

2. Chạy preflight kiểm tra cấu hình và xác thực API (không tạo VM):

```powershell
.\spikes\01_instabox_openswe\.venv\Scripts\python.exe spikes\01_instabox_openswe\run_phase2_spike.py --preflight-only
```

3. Chạy Phase 2 live mặc định trên `fixtures/mini_bug_repo` (vai trò `dev`, verify bằng VM sạch từ `pre_agent_snapshot_id`):

```powershell
.\spikes\01_instabox_openswe\.venv\Scripts\python.exe spikes\01_instabox_openswe\run_phase2_spike.py --artifact-dir spikes/01_instabox_openswe/artifacts/phase2_run
```

4. Hoặc dùng `--verify-mode restore` nếu muốn restore snapshot trên cùng VM thay vì tạo VM verify riêng:

```powershell
.\spikes\01_instabox_openswe\.venv\Scripts\python.exe spikes\01_instabox_openswe\run_phase2_spike.py --verify-mode restore --artifact-dir spikes/01_instabox_openswe/artifacts/phase2_restore
```

Tùy chọn chính: `--role <role>`, `--model <id>`, `--repo-dir <path>` hoặc `--repo-url <https-url>`, `--base-ref <ref>`, `--task <prompt>`, `--python-bin <abs-posix-path>`, `--allow-config-changes`, `--tdd-exempt`, `--max-agent-steps <5..200>` (mặc định `60`), `--snapshot-timeout <secs>`, `--keep-vm`.
Artifact xuất ra khi dùng `--artifact-dir`: `patch.diff`, `red_junit.xml`, `post_junit.xml`, `verification_report.json`.
Mã thoát của `run_phase2_spike.py`: `0` (`PASS`), `1` (lỗi runtime / `CleanupIncompleteError`), `2` (lỗi cấu hình), `3` (`REJECT`), `4` (`NEEDS_HUMAN_REVIEW`).
