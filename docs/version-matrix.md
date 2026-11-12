# Version Matrix & Integration Feasibility Report

> **Hướng model hiện tại — 9Router + nhiều model:** Model ID không có prefix đi
> qua 9Router mặc định. Có thể chọn `--models` hoặc mapping vai trò
> `NINE_ROUTER_ROLE_MODELS` + `--roles`. Không hardcode model Anthropic mặc định.
> **139 test offline PASS; Phase 1 live PASS cho Dev và Reviewer** theo log người
> dùng cung cấp (VM đã xóa; BA/Tester chưa chạy). Harness **Phase 2 đạt OFFLINE PASS
> / LIVE NOT RUN** (chỉ kiểm tra luồng bugfix + snapshot + Hard Gates của Spike 01,
> chưa phải AI SDLC hoàn chỉnh). Xem [kết quả live](phase-1-live-smoke.md),
> [thiết kế model routing](model-routing.md) và [hướng dẫn chạy](../spikes/01_instabox_openswe/README.md).

> **Cập nhật 2026-11-13 — 9Router:** Runner Phase 1 hỗ trợ `--model 9router:<model-id>`
> qua `langchain-openai==1.7.4` và Chat Completions. URL/key đọc từ
> `NINE_ROUTER_BASE_URL` / `NINE_ROUTER_API_KEY` (fallback `OPENAI_BASE_URL` /
> `OPENAI_API_KEY`). Xem [hướng dẫn chạy](../spikes/01_instabox_openswe/README.md).
> Toàn bộ **100 test offline PASS**, gồm kiểm tra HTTP giả lập cho URL, model ID,
> authorization và tool calling. Đây là mốc offline trước khi bổ sung multi-model;
> mốc gần nhất sau đó là 127 test offline, và hiện tại là **139 test offline PASS**
> sau khi bổ sung harness Phase 2 (Phase 2 vẫn `LIVE NOT RUN`).

> **Ngày kiểm tra mã nguồn:** 2026-11-13  
> **Trạng thái kiểm thử thực tế:** Phase 1 `LIVE PASS` với hai model Dev/Reviewer
> theo log người dùng cung cấp; Phase 2 (`run_phase2_spike.py` + `phase2_verifier.py`)
> đạt **`OFFLINE PASS`** (bao gồm test unit, `git`/`pytest` thật trên fixture và
> runner mock InstaBox) nhưng **`LIVE NOT RUN`** trên VM thật.
> Các dự án khác vẫn giữ trạng thái riêng trong bảng.

---

## 1. Quy ước tên phương án tích hợp cho lõi thực thi (Open SWE / Deep Agents)

Để tránh nhầm lẫn xuyên suốt tài liệu và quá trình làm spike, chúng ta thống nhất tên gọi **2 phương án ứng viên** như sau:

* **Phương án 1A (`deepagents` + `InstaBoxSandbox` + Prompt lấy cảm hứng từ Open SWE):**
  - Sử dụng trực tiếp thư viện [`langchain-ai/deepagents`](https://github.com/langchain-ai/deepagents) (`deepagents==0.7.36`, MIT License, hỗ trợ Python `>=3.11,<4.0`), viết adapter `InstaBoxSandbox(BaseSandbox)` nối vào InstaBox, và tái sử dụng mẫu system prompt của Open SWE.
  - **Lưu ý phạm vi:** Đây là một **coding agent tùy biến** dùng cho Spike kiểm chứng kết nối thực thi trong InstaBox. Nó **chưa có sẵn** các tính năng cấp hệ thống của Open SWE như: quản lý phiên nhiều lượt qua webhook, luồng Reviewer/Review-Scout chuyên biệt, tích hợp GitHub App và tự động tạo/cập nhật PR. Sau khi chạy xong Spike, ta sẽ đánh giá riêng xem cần tự xây phần nào ở Orchestrator hay quay lại Phương án 1B.
* **Phương án 1B (Pin Open SWE tại commit `5308cc58` — Pre-MDA):**
  - Pin repo `langchain-ai/open-swe` tại commit `5308cc586a9f8bbc1f8f7a83732b20a8cdf46211` (2026-10-16, commit cuối cùng trước khi chuyển sang Managed Deep Agents ở PR #4159).
  - Giữ được cấu trúc server/graph gốc của Open SWE và cơ chế đăng ký provider qua `SANDBOX_TYPE`, nhưng đòi hỏi `Python >= 3.14` và kéo theo các phụ thuộc hạ tầng của Open SWE.

---

## 2. Bảng tổng hợp phiên bản & môi trường (Version Matrix)

| Thành phần | Repo / Package | Nhánh / Commit / Version khảo sát | Yêu cầu Python (theo khai báo) | License | Trạng thái Smoke Test | Ghi chú & Rủi ro cần kiểm chứng |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| **Open SWE (`main` - Robin)** | `langchain-ai/open-swe` | `main` @ `993082970f0894a80180ad6671dfe40b95c0ce5d` (2026-11-12) | `>=3.14` | MIT *(loại trừ `vendor/`)* | `NOT RUN` | Phụ thuộc `managed-deepagents` (`vendor/`) và chỉ kết nối LangSmith Sandbox do MDA tạo. Không dùng nguyên trạng cho MVP. |
| **Open SWE (Phương án 1B)** | `langchain-ai/open-swe` | Commit `5308cc586a9f8bbc1f8f7a83732b20a8cdf46211` (2026-10-16) | `>=3.14` | MIT | `NOT RUN` | Bản cuối trước MDA; còn `SANDBOX_TYPE` registry. Máy host hiện mới có Python `3.12` & `3.13` (cần cài `3.14` nếu muốn test 1B). |
| **Deep Agents (Phương án 1A)** | `deepagents` (PyPI) / `langchain-ai/deepagents` | `deepagents==0.7.36` (PyPI) | `>=3.11, <4.0` | MIT | `OFFLINE PASS` (139 tests toàn spike) / `LIVE PASS` (Phase 1, log người dùng) / `Phase 2: OFFLINE PASS, LIVE NOT RUN` | Phase 1: Agent Dev và Reviewer ghi file riêng khớp chính xác bytes trên InstaBox qua 9Router local. Phase 2 (`run_phase2_spike.py`, `phase2_verifier.py`): đã kiểm tra offline (Hard Gates + Red -> Green trên fixture thật + runner mock), chưa chạy live trên InstaBox và chưa phải AI SDLC hoàn chỉnh. |
| **InstaBox SDK** | `instabox-sdk` (PyPI) | `instabox-sdk==0.5.2` (PyPI) | `>=3.9` (tài liệu ghi `3.9–3.13`) | Dịch vụ Cloud (SDK dùng `httpx`) | `OFFLINE PASS` (139 tests toàn spike) / `LIVE PASS` (Phase 1, log người dùng) / `Phase 2: OFFLINE PASS, LIVE NOT RUN` | Live PASS (Phase 1): create, execute/timeout, binary round-trip, BaseSandbox file tools, cleanup VM. Phase 2 snapshot (`snapshots.create/get/delete`), `machines.restore` và fallback/recovery hiện được kiểm tra offline, chưa chạy live. |
| **MetaGPT** | `FoundationAgents/MetaGPT` | `main` @ `11cdf466d042aece04fc6cfd13b28e1a70341b1f` (2026-01-21) | Không ghi rõ trần trong `setup.py`, nhưng pin `aiohttp==3.8.6`, `numpy~=1.26.4` | MIT | `NOT RUN` | `aiohttp==3.8.6` không có wheel prebuilt cho Python 3.12+ trên Windows (dễ lỗi build C extension). Cần smoke test cài đặt thực tế (có thể cần Python 3.10/3.11 hoặc chỉ tách phần gọi LLM/ActionNode). |
| **ChatDev 2.0 (DevAll)** | `OpenBMB/ChatDev` | `main` @ `464a681ba7bda869db97a50c5fa707c9a0fb57a0` (2026-10-30) | `>=3.12, <3.13` | Apache-2.0 | `NOT RUN` | Khớp với Python `3.12` có sẵn trên máy. Cần smoke test chạy một workflow YAML tối giản không bật tool thực thi code. |
| **ChatDev 1.0 (Legacy)** | `OpenBMB/ChatDev` | `chatdev1.0` @ `31fd994416a251ecdeb1f0a73c329271743bfb56` | `3.9 – 3.11` | Apache-2.0 | `NOT RUN` | Nhánh dự phòng. Pin `numpy==1.24.3`, `openai==1.47.1`. |

> [!IMPORTANT]
> **Môi trường Python thực tế trên máy hiện tại (`py -0p`):**
> - Đang có sẵn: **Python 3.13.9** (mặc định) và **Python 3.12**.
> - Chưa có `uv` và chưa có `Python 3.14`.
> - Vì các dự án pin cứng phiên bản `fastapi`, `pydantic`, `openai` khác nhau, mỗi thành phần **bắt buộc dùng virtualenv (`venv`) riêng biệt**.

---

## 3. Đối chiếu mã nguồn thực tế theo đúng Commit SHA (Xác minh các điểm nghi vấn)

### 3.1. Xác minh `docs/CUSTOMIZATION.md` và `openswe/sandboxes/providers/` của Open SWE
Đối chiếu trực tiếp qua GitHub API tại 3 mốc commit SHA:

1. **Tại commit `b2eef1a0bc50c2e212bc299c23474c11876e3a5d` (2026-10-22) và `5308cc586a9f8bbc1f8f7a83732b20a8cdf46211` (2026-10-16):**
   - Thư mục `openswe/sandboxes/providers/` có đầy đủ: `langsmith.py`, `daytona.py`, `e2b.py`, `modal.py`, `runloop.py`, `local.py`, và `registry.py` (với biến môi trường `SANDBOX_TYPE`).
   - File `docs/CUSTOMIZATION.md` ở các commit này có mục *"Using a different sandbox provider"* hướng dẫn cấu hình `SANDBOX_TYPE`.
2. **Tại commit `a55e6ab290ff7c8a770bcbfa3a9b43a3a8c28ff8` (2026-10-28 — PR #4629: `fix(sandboxes)!: drop non-LangSmith sandbox providers; only MDA creates thread sandboxes`):**
   - Commit này xóa 5 file provider (`daytona.py`, `e2b.py`, `local.py`, `modal.py`, `runloop.py`), xóa 90 dòng hướng dẫn `SANDBOX_TYPE` khỏi `docs/CUSTOMIZATION.md`, và sửa `registry.py`.
3. **Tại commit `main` hiện tại (`993082970f0894a80180ad6671dfe40b95c0ce5d`, 2026-11-12):**
   - Thư mục `openswe/sandboxes/providers/` chỉ còn 4 file: `__init__.py`, `langsmith.py` (43,726 bytes), `langsmith_api.py` (2,850 bytes), và `registry.py` (1,237 bytes).
   - Nội dung `openswe/sandboxes/providers/registry.py` tại `99308297` chỉ có hàm `connect_existing_sandbox(sandbox_id: str)` gọi thẳng `create_langsmith_sandbox(sandbox_id)` cùng docstring: *"Managed Deep Agents creates every thread's sandbox; the backend only reconnects to one by its id, so nothing here can create a sandbox."*
   - File `docs/CUSTOMIZATION.md` tại `99308297` (dòng 1–65) mở đầu bằng `# Customization Guide — Robin is designed to be forked...` và tại mục `### Who creates sandboxes` ghi rõ chỉ dùng LangSmith sandbox do MDA tạo.

### 3.2. Kiểm tra `BaseSandbox` trong `deepagents` (Phá bỏ giả thuyết "chỉ cần override 4 hàm")
Đọc mã nguồn `libs/deepagents/deepagents/backends/sandbox.py` và `protocol.py` cho thấy:
Mặc dù `BaseSandbox` chỉ đánh dấu `@abstractmethod` ở 4 nơi (`id`, `execute`, `upload_files`, `download_files`), việc **chỉ override 4 hàm cơ bản là chưa đủ cho thực tế vận hành với InstaBox** vì các lý do sau:

1. **Xử lý Timeout và Reconnect có giới hạn khi máy tự ngủ (`auto-suspend`):**
   - InstaBox mặc định tự pause VM sau 30 phút không hoạt động. Nếu gọi `client.machines.exec(...)` hoặc `client.files.*` khi VM đã chuyển sang trạng thái paused/stopped, InstaBox trả lỗi `409 ConflictError (code="machine_not_running")`.
   - **Quy tắc retry an toàn (Bounded Retry):** Chỉ tự động gọi `ensure_running(machine_id)` và thử lại (tối đa 1 lần) khi nhận đúng mã lỗi `machine_not_running` (nghĩa là lệnh/thao tác chắc chắn chưa được thực thi). Nếu gặp lỗi timeout (`httpx.TimeoutException`, `WaitTimeoutError`), mất kết nối mạng gián đoạn sau khi gửi request, hoặc lỗi `5xx` không rõ trạng thái, Adapter **tuyệt đối không tự chạy lại lệnh `execute`** để tránh lặp side effect (ví dụ chạy lại thao tác ghi/xóa).
2. **Hàm `execute(self, command: str, *, timeout: int | None = None) -> ExecuteResponse`:**
   - `BaseSandbox` dùng `execute_accepts_timeout(cls)` (`inspect.signature`) để kiểm tra xem `execute` có nhận tham số keyword `timeout` hay không. Chữ ký hàm `execute` **phải khai báo rõ `*, timeout: int | None = None`**.
   - **Giới hạn timeout 600 giây của `machines.exec()`:** Trong `instabox-sdk==0.5.2`, `machines.exec` giới hạn `timeout` tối đa 600 giây (`1..600`). Adapter phải có `default_timeout` rõ ràng (ví dụ `120` giây) và kiểm tra đầu vào: từ chối `timeout <= 0` hoặc kẹp/báo lỗi rõ ràng khi vượt quá `600` giây.
   - **Cấu trúc thực tế của `ExecResult` (không có `oom_killed`):** Kiểm tra trực tiếp `instabox.ExecResult` trong SDK `0.5.2` cho thấy dataclass này chỉ có 6 trường: `stdout: str`, `stderr: str`, `exit_code: int`, `timed_out: bool`, `truncated: bool`, `signal: str` (`from_dict` bỏ qua trường `oomKilled`). Không được kết luận OOM chỉ từ `exit_code == 137` hay `signal == "SIGKILL"`; khi tiến trình bị kill bởi signal hoặc exit 137, Adapter ghi chú *"tiến trình bị kill (signal/exit 137), chưa xác định nguyên nhân (có thể do OOM hoặc bị tín hiệu ngoài)"* (hoặc tra cứu thêm `machines.get(id)` nếu cần xác minh).
   - Kết quả `ExecuteResponse` của `deepagents` nhận `output: str` (gộp `stdout` + `stderr` + ghi chú khi `timed_out=True` hoặc có `signal`), `exit_code: int | None`, và `truncated: bool` (lấy từ `ExecResult.truncated`).
3. **Hàm `upload_files` và `download_files` (Tách bạch lỗi file vs. lỗi hạ tầng & Xử lý nhánh Fallback qua `exec`):**
   - Docstring của `BaseSandbox` quy định `upload_files(self, files: list[tuple[str, bytes]]) -> list[FileUploadResponse]` và `download_files(self, paths: list[str]) -> list[FileDownloadResponse]` hỗ trợ partial success cho các lỗi cấp độ file (`"file_not_found"`, `"permission_denied"`, `"is_directory"`, `"invalid_path"`).
   - **Nhánh Fallback qua `exec` trong `instabox-sdk==0.5.2`:** Kiểm tra mã nguồn `Files.read_bytes()` và `Files.write()` cho thấy khi server không có HTTP file routes (trả về 405 hoặc 404 `"route not found"`), SDK tự động chuyển sang truyền dữ liệu base64 theo từng chunk qua `machines.exec` (`_exec_size`, `_exec_chunks`, `_write_exec`). Ở nhánh fallback này, các lỗi như quyền truy cập (`Permission denied`), đường dẫn là thư mục khi đọc, hoặc đường dẫn không hợp lệ sẽ trả về `APIError(code="transfer_failed", message="in the sandbox: ...")` (hoặc `code="not_a_file"`). Adapter phải phân tích cả lỗi từ nhánh HTTP trực tiếp lẫn nhánh fallback `_write_exec`/`_exec_size` và kiểm thử offline cả hai nhánh.
   - **Tách lỗi file vs. lỗi hạ tầng:** Chỉ chuyển thành `FileUploadResponse(error=...)` / `FileDownloadResponse(error=...)` đối với các lỗi thao tác file rõ ràng. Nếu gặp lỗi hạ tầng hoặc xác thực (`AuthenticationError`, `PermissionDeniedError` do `missing_scope`, `NotFoundError` cấp độ máy `machine_not_found`, `APIConnectionError`, `ServerError`, hoặc VM không thể đánh thức), Adapter **phải raise exception nguyên bản** để Orchestrator dừng run, tuyệt đối không báo nhầm thành `invalid_path` hay lỗi từng file.
4. **Thư mục làm việc mặc định (`cwd`):**
   - Các lệnh `grep`/`glob` của `BaseSandbox` khi truyền `path=None` sẽ tìm trên `"."` (thư mục hiện hành của lệnh `exec`). Vì mỗi lần gọi `POST /v1/machines/{id}/exec` là một tiến trình `bash -lc` mới (mặc định ở `$HOME`), nếu repo được clone vào `/data/workspace/repo`, Adapter cần truyền `cwd=self.workdir` vào `client.machines.exec(..., cwd=self.workdir)` và chuẩn hóa đường dẫn tương đối trong `upload_files`/`download_files` về `self.workdir`.

---

## 4. Quy tắc Hard Gate & Quy trình Kiểm chứng Độc lập (Independent Verification)

Để đảm bảo tính nhất quán và không phụ thuộc vào môi trường đã bị agent can thiệp, quy tắc kiểm tra ở Orchestrator (và áp dụng ngay trong Spike) được thiết lập như sau:

### 4.1. Quy tắc bảo vệ Test & Cấu hình (Nhất quán cho MVP)
1. **Chính sách đối với file test cũ (`existing_test_files` tại `base_commit_sha`):**
   - **Quy tắc đơn giản cho MVP:** Agent **chỉ được thêm file test mới**. Mọi thay đổi (sửa hoặc xóa) trên bất kỳ file test nào đã tồn tại ở `base_commit_sha` đều tự động đánh dấu `NEEDS_HUMAN_REVIEW` (không tự động cho qua).
2. **Chính sách đối với cấu hình chạy test và môi trường (`protected_config_paths`):**
   - Đưa các file cấu hình test/build (`pyproject.toml`, `pytest.ini`, `setup.cfg`, `tox.ini`, `conftest.py` gốc, `.github/**`, script CI) vào danh sách kiểm soát:
     - Nếu task không yêu cầu sửa dependency/config: xếp vào `forbidden_paths` (`REJECT` ngay nếu chạm vào).
     - Nếu task có yêu cầu: bắt buộc chuyển `NEEDS_HUMAN_REVIEW`.

### 4.2. Quy trình thu thập Baseline và Kiểm tra Độc lập (Pre/Post Verification)
Tất cả các bước dưới đây do **script của Orchestrator/Spike gọi trực tiếp qua InstaBox API**, hoàn toàn tách biệt với phiên chạy của agent:

1. **Bước Trước khi Agent chạy (Baseline Capture tại `base_commit_sha`):**
   - Ghi nhận `base_commit_sha` (`git rev-parse HEAD`).
   - Lập danh sách toàn bộ file đang có trong repo (`git ls-files`), tách riêng tập `existing_test_files` và hash SHA-256 của các file cấu hình (`pyproject.toml`, `pytest.ini`, `conftest.py`, `uv.lock`/`requirements.txt`).
   - Chạy bộ test hiện có bằng đường dẫn tuyệt đối của test runner (ví dụ `/usr/bin/python3 -m pytest --junitxml=/tmp/baseline_junit.xml`) và lưu lại **bản đồ trạng thái từng test ID**: `baseline_status[test_id] -> {passed | failed | skipped | error}` (không giả định mọi baseline test đều đang pass).
2. **Bước Sau khi Agent kết thúc (Independent Verification):**
   - **Thu thập toàn bộ thay đổi trên working tree và lịch sử git** (tránh trường hợp agent đã `git commit`, `git mv` hoặc tạo file untracked):
     - Nếu agent đã tạo commit mới: lấy diff từ `base_commit_sha` đến `HEAD` cộng với working tree (`git status --porcelain -uall`, `git diff --name-status -M <base_commit_sha>`).
     - Xuất toàn bộ bản vá (patch) ra ngoài VM.
   - **Chạy kiểm tra trong môi trường sạch xuất phát từ Snapshot trước khi Agent chạy (Pre-Agent Snapshot Verification):**
     - Fork máy sau khi agent chạy hoặc chỉ tạo git branch sạch trên cùng máy **vẫn giữ nguyên các thay đổi ngoài Git** (như package cài đè vào `.venv`, binary hệ thống bị sửa, biến môi trường, tiến trình ngầm).
     - Do đó, ở Bước 1 (trước khi giao máy cho Agent), Orchestrator phải chụp một snapshot gốc (`pre_agent_snapshot_id`) hoặc khởi tạo môi trường verify từ đúng snapshot/template ban đầu trước khi agent chạm vào máy, sau đó chỉ apply bản patch hợp lệ và chạy `/path/to/clean/pytest --junitxml=/tmp/post_junit.xml`.
   - **So sánh trạng thái `baseline_status` vs `post_status` & Quy tắc `tdd_exempt`:**
     - **Không thoái lui (No Regression):** Mọi `test_id` có trong `baseline_status` không được chuyển từ `passed` sang `failed`/`error`/`skipped`, và không được biến mất khỏi báo cáo.
     - **Test mới & Ngoại lệ `tdd_exempt`:** Mặc định phải có ít nhất 1 `test_id` mới nằm trong file test mới fail vì đúng lỗi ở bước Red và chuyển sang `passed` ở bước Green. Cờ `tdd_exempt: true` **chỉ miễn yêu cầu có test fail trước khi sửa (bước Red)**, tuyệt đối **không miễn kiểm chứng kết quả sau khi sửa**; báo cáo thực thi bắt buộc phải ghi rõ phương pháp và bằng chứng kiểm chứng thay thế (ví dụ: kết quả chạy suite hiện có, kiểm tra build/lint/schema, hoặc hướng dẫn kiểm tra thủ công có log đính kèm).

---

## 5. Thứ tự thực hiện tiếp theo

1. **[Hoàn thành]** Cập nhật và xác minh `docs/version-matrix.md` với trạng thái `NOT RUN` và đối chiếu đúng commit SHA.
2. **[Đã xong phần Offline]** **Viết Adapter tối thiểu (`spikes/01_instabox_openswe/instabox_sandbox.py`) & Runner (`spikes/01_instabox_openswe/run_live_vm_spike.py`):**
   - Tạo virtualenv riêng cho Spike, cài `instabox-sdk==0.5.2` và `deepagents==0.7.36`, kiểm tra chữ ký thực tế của hai thư viện.
   - Viết `InstaBoxSandbox(BaseSandbox)` xử lý đầy đủ `workdir`, `timeout` (chung validation cho constructor và `execute`), `truncated`, `oomKilled`, `upload_files`/`download_files` (partial success + binary + giữ nguyên exception khi mã lỗi mơ hồ), pre-validation trước khi gọi API và `SandboxInitializationError` kèm `machine_id` trong `create_or_reconnect`, và reconnect (`ensure_running`).
   - Viết `run_live_vm_spike.py` nhận key từ biến môi trường, kiểm tra thiếu cấu hình trước khi tạo VM, tách rõ trạng thái `--preflight-only` + `--skip-api-preflight` (chỉ kiểm tra cấu hình local, chưa xác thực API), dùng `create(..., wait=False)` + recovery lookup khi lỗi mơ hồ (không tạo thêm VM), trả mã lỗi khác `0` kèm `machine_id` nếu xóa VM thất bại, và kiểm tra agent smoke bằng tên file duy nhất theo run + so sánh bytes chính xác.
3. **Phase 1: Chạy Live Smoke trên VM thật (`spikes/01_instabox_openswe/run_live_vm_spike.py`) — Trạng thái: `LIVE PASS` cho Dev và Reviewer theo log người dùng:**
   - Sandbox smoke, hai agent ghi file riêng và cleanup VM đều PASS. BA/Tester chưa chạy.
   - Phạm vi hiện tại của runner **chỉ** kiểm tra thao tác sandbox và agent ghi file; chưa thực hiện `pre_agent_snapshot_id`, chạy `pytest` repo thử nghiệm hay verify độc lập. Xem [kết quả live](phase-1-live-smoke.md).
4. **Phase 2: Chạy 1 task bugfix & Verify độc lập trên repo thử nghiệm — Trạng thái: `OFFLINE PASS / LIVE NOT RUN`:**
   - Đã cài đặt `spikes/01_instabox_openswe/run_phase2_spike.py`, `phase2_verifier.py`, `spike_common.py` và repo fixture `fixtures/mini_bug_repo`: ghi nhận `base_commit_sha`, baseline `pytest` JUnit XML, hash file cấu hình bảo vệ, tạo `pre_agent_snapshot_id`, chạy Deep Agent sửa bug, sau đó kiểm tra Hard Gates và verify độc lập Red -> Green (mặc định bằng VM mới từ `pre_agent_snapshot_id` hoặc `--verify-mode restore`), cuối cùng xóa VM và snapshot nghiêm ngặt.
   - Đã kiểm chứng offline (unit test Hard Gates/JUnit/git `-z`, chạy `git` + `pytest` thật trên `fixtures/mini_bug_repo`, và lifecycle mock InstaBox), **chưa chạy live trên VM InstaBox thật** (`LIVE NOT RUN`). Phạm vi chỉ là Spike 01 bugfix + verify độc lập, chưa phải hệ thống AI SDLC hoàn chỉnh.
