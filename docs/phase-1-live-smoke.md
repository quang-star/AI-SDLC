# Phase 1: kết quả live smoke qua 9Router local

Nguồn bằng chứng: log chạy thực tế do người dùng cung cấp trong chat ngày
2026-11-13. Không chạy lại API để xác nhận; không lưu credential.

- Run ID: `5ffea77cc4c5`
- Machine ID: `m_agq5cdec4tlnu3b6n5vxyn24je`
- Operation ID: `op_agq5cdec4twiivt2qostqm5s2a`
- Workdir: `/data/workspace/spike01`
- Gateway: `http://127.0.0.1:20128/v1`
- Lệnh: `run_live_vm_spike.py --with-agent --roles dev reviewer`

| Kiểm tra | Kết quả theo log |
| --- | --- |
| Tạo máy, chuẩn bị workdir, chạy shell | PASS |
| Timeout `sleep 3`, timeout=1 | PASS, exit code 124 |
| Binary/text upload-download | PASS, HTTP file routes |
| BaseSandbox write/edit/read/ls/glob/grep/delete | PASS |
| Dev: `cx/gpt-6.1-sol` | PASS, agent ghi file khớp chính xác 30 bytes |
| Reviewer: `ag/claude-sonnet-4-6` | PASS, agent ghi file khớp chính xác 30 bytes |
| Cleanup VM mới tạo | PASS, log xác nhận deleted |
| BA: `ag/gemini-3.8-flash` | NOT RUN |
| Tester: `glm/glm-5.3-flash` | NOT RUN |

Các probe chẩn đoán đã ghi nhận:

- File thiếu: `NotFoundError`, code `not_found`, HTTP 404.
- Đọc thư mục: `ConflictError`, code `not_a_file`, HTTP 409.

Đây là kết quả kết nối, tool calling và file I/O. Nhãn `reviewer` trong lần chạy
này chỉ chọn model: chưa thực hiện code review thật hoặc gọi ChatDev.
Chưa kiểm chứng toàn bộ AI SDLC, MetaGPT/ChatDev, vòng TDD, pre-agent snapshot,
baseline/post pytest hay verify độc lập (Phase 2).
