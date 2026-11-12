# Cấu hình model cho AI SDLC qua 9Router

## Quyết định hiện tại

9Router là gateway LLM mặc định. Model selection tách khỏi engine thực thi:
InstaBox chạy code/test; model chạy thông qua endpoint Chat Completions của
9Router từ host đang chạy Python. Không đưa router key vào VM.

| Vai trò dự kiến | Thành phần dự kiến | Cấu hình |
| --- | --- | --- |
| BA / viết spec | MetaGPT adapter | `ba` |
| Thiết kế / lập kế hoạch | MetaGPT hoặc coding adapter | `architect`, `planner` nếu cần |
| Viết và sửa code | Deep Agents + InstaBox (phương án 1A) | `dev` |
| Phản biện spec / review | ChatDev adapter | `reviewer` |
| Đề xuất test / phân tích lỗi | ChatDev hoặc testing adapter | `tester` |

Mỗi vai trò có thể dùng một model riêng hoặc cùng model. Model ID phải là route
thật trong 9Router; có thể là model provider hoặc combo. Router credential và
endpoint dùng chung, còn model ID lấy từ mapping. Chưa kết nối MetaGPT/ChatDev:
bảng này là contract cấu hình cho các adapter sẽ triển khai, không phải workflow
đã chạy.

## Phần đã triển khai

`spikes/01_instabox_openswe/model_routing.py` cung cấp `ModelTarget` và
`select_model_targets()` để chọn model theo tên vai trò, danh sách CLI hoặc model
mặc định. Runner dùng kết quả này để tạo agent riêng và thử tool calling lần lượt
trên VM. Mỗi lượt có định danh file riêng; kết quả thất bại được tổng hợp trước
khi cleanup. Một lỗi model không được tính thành PASS nhờ kết quả model khác.

Thứ tự chọn:

1. `--model`, `--models` hoặc `--roles` (chỉ chọn một).
2. `NINE_ROUTER_MODEL`, fallback `DEEPAGENTS_MODEL`.
3. Toàn bộ mapping `NINE_ROUTER_ROLE_MODELS` dạng JSON.

Bare ID tự thêm `9router:`; prefix LangChain rõ ràng vẫn được giữ để tương thích.
Chọn `9router:<id>` nếu route ID chứa dấu `:`.

Xem [hướng dẫn chạy và cấu hình nhiều model](../spikes/01_instabox_openswe/README.md).
Kết quả hiện tại: **139 test offline PASS** (gồm Phase 1 và harness Phase 2;
Phase 2 vẫn `LIVE NOT RUN`). Theo log người dùng cung cấp,
**Phase 1 live PASS** với Dev `cx/gpt-6.1-sol` và Reviewer
`ag/claude-sonnet-4-6`, gọi qua 9Router local và ghi file trên InstaBox; VM đã xóa.
BA/Tester chưa chạy. Xem [bằng chứng và giới hạn](phase-1-live-smoke.md).
Các lượt chạy này chưa đánh giá chất lượng model hay tính đúng của toàn bộ SDLC.
