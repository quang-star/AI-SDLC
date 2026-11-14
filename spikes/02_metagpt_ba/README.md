# Spike 02 — MetaGPT BA Adapter (ActionNode + 9Router)

Tạo `TaskSpec` gắn với `RepoContext` từ yêu cầu ngôn ngữ tự nhiên thông qua role `ba` trên 9Router, áp dụng cấu trúc `ActionNode` + `[CONTENT]...[/CONTENT]` của MetaGPT và dừng ở trạng thái `draft` để con người duyệt (`approve` / `reject`).

## 1. Kết quả smoke test cài đặt `metagpt==0.8.2`

Kiểm tra thực tế trên Python `3.11.18` (virtualenv tách riêng `spikes/02_metagpt_ba/.venv`):

- `pip install metagpt==0.8.2` **thất bại** do PyPI không còn gói `lancedb==0.4.0` (chỉ còn `0.14.0+`).
- Kể cả khi giải nén `metagpt==0.8.2` bằng `--no-deps`, `import metagpt` lỗi ngay trên Windows vì `metagpt/_compat.py` import `semantic_kernel`, còn `metagpt/actions/__init__.py` và `metagpt/provider/__init__.py` kéo theo ~37 SDK không liên quan (`playwright`, `nbclient`, `gymnasium`, `boto3`, `zhipuai`, `qianfan`, `dashscope`, ...).
- Vì vậy, đúng như phương án đã dự phòng trong `docs/version-matrix.md`, Spike này tách riêng mẫu `ActionNode` + `ProductManager` + `[CONTENT]...[/CONTENT]` + vòng self-repair sang `action_node.py` và `ba_agent.py` dùng trực tiếp `openai` + `pydantic` + `httpx`.

## 2. Tạo virtualenv & chạy test offline

```powershell
# Tạo lại từ Conda Python 3.11 hoặc Python >= 3.11 sẵn có:
py -3.12 -m venv .\spikes\02_metagpt_ba\.venv
.\spikes\02_metagpt_ba\.venv\Scripts\python.exe -m pip install -r .\spikes\02_metagpt_ba\requirements.txt

# Nếu dùng môi trường Conda đang tạo sẵn ở spikes/02_metagpt_ba/.venv:
.\spikes\02_metagpt_ba\.venv\python.exe -m pytest .\spikes\02_metagpt_ba -q
```

## 3. Chạy live BA smoke test với 9Router + `mini_bug_repo`

Nạp cấu hình local 9Router trước (lấy từ `spikes/01_instabox_openswe/setup_local.ps1`):

```powershell
. .\spikes\01_instabox_openswe\setup_local.ps1
```

Kiểm tra preflight riêng cho role `ba`:

```powershell
.\spikes\02_metagpt_ba\.venv\python.exe .\spikes\02_metagpt_ba\run_ba_spike.py --preflight-only
```

Sinh `TaskSpec` cho `mini_bug_repo`:

```powershell
.\spikes\02_metagpt_ba\.venv\python.exe .\spikes\02_metagpt_ba\run_ba_spike.py `
  --task "Fix billing discount validation: negative discount_percent should raise ValueError, and discount over 100 percent should clamp or reject cleanly without breaking existing positive discounts." `
  --repo-dir .\spikes\01_instabox_openswe\fixtures\mini_bug_repo
```

Lệnh trên xuất artifact vào `spikes/02_metagpt_ba/artifacts/ba_run/`:
- `repo_context.json` — ngữ cảnh repo, file theo dõi, Hard Gate `existing_test_files` & `protected_config_paths`.
- `task_spec.json` — bản `TaskSpec` ở trạng thái `draft`, có `spec_sha256`.
- `task_spec_review.md` — bản Markdown dễ đọc để bạn kiểm tra.
- `ba_run_metadata.json` — model, số lần tự sửa (`repair_attempts`), prompt SHA-256 (không lưu API key).

Duyệt hoặc từ chối spec vừa sinh:

```powershell
# Duyệt:
.\spikes\02_metagpt_ba\.venv\python.exe .\spikes\02_metagpt_ba\run_ba_spike.py `
  --approve .\spikes\02_metagpt_ba\artifacts\ba_run\task_spec.json `
  --reviewer "owner" --comment "LGTM"

# Từ chối để sửa lại (tăng spec_version):
.\spikes\02_metagpt_ba\.venv\python.exe .\spikes\02_metagpt_ba\run_ba_spike.py `
  --reject .\spikes\02_metagpt_ba\artifacts\ba_run\task_spec.json `
  --reviewer "owner" --comment "Add explicit edge case for 100.01% discount"
```
