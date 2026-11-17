# Phân công 3 người / 3 nhánh

**Phạm vi chung:** bản demo này dùng 100% thiết bị mô phỏng Wokwi và dịch vụ phần mềm chạy local. Không có phần cứng thật; mọi mục khóa/UPS/cảm biến/nguồn thật trong tài liệu tổng hợp là tham khảo cho hướng mở rộng, không phải điều kiện nghiệm thu demo.

Điểm xuất phát chung: `main` tại commit có Wokwi, Backend và kế hoạch hiện tại. Mỗi người làm trên nhánh của mình, mở Pull Request vào `main`; không commit mật khẩu, chứng chỉ riêng, `include/secrets.h`, database hoặc thư mục `.pio`.

| Người | Nhánh | Phạm vi sở hữu | Sản phẩm bàn giao |
| --- | --- | --- | --- |
| 1 — Firmware & Wokwi | `feature/firmware-wokwi` | `src/`, `include/`, `platformio.ini`, `diagram.json`, `wokwi.toml` | Mô phỏng quét thẻ → xung mở khóa bằng LED → cửa mở/đóng bằng switch; xử lý timeout/lỗi/event; sơ đồ linh kiện và GPIO Wokwi cho một ngăn |
| 2 — Backend & Dashboard | `feature/backend-dashboard` | `backend/`, giao diện quản trị mới | API/DB quản lý đơn-ngăn-quyền-phiên; Dashboard tối thiểu; test nghiệp vụ và bản tin trùng |
| 3 — MQTT, tích hợp & kiểm thử | `feature/integration-qa` | `broker/`, tài liệu, script chạy/test tích hợp, bằng chứng thử nghiệm | Broker TLS/ACL mẫu được kiểm chứng; kịch bản chạy Wokwi trọn tuyến; bảng kết quả KT và hướng dẫn tái hiện |

## Người 1 — Firmware & Wokwi

1. Chạy Wokwi for VS Code với Private IoT Gateway; kiểm tra chân MFRC522, công tắc cửa và LED trong `diagram.json`.
2. Dùng thẻ đỏ `AABBCCDD` để giao và thẻ xanh lá `11223344` để nhận; đối chiếu Serial Monitor với phản hồi MQTT. Sửa lỗi quan sát được.
3. Bổ sung chống lặp lệnh, timeout phiên, xử lý cửa bỏ ngỏ/cửa mở bất thường và trạng thái an toàn khi reset. Gửi sự kiện có `event_id` bền vững nếu triển khai CN06.
4. Xác nhận sơ đồ nối dây ảo và trạng thái GPIO trong Wokwi. Thiết kế mạch/khóa thật nằm ngoài phạm vi demo hiện tại.

**Đạt khi:** build `pio run -e wokwi` thành công; video/log Wokwi cho thấy hai thẻ mở đúng giai đoạn, thẻ sai không mở; GPIO mô phỏng có trạng thái đúng.

## Người 2 — Backend & Dashboard

1. Hoàn thiện mô hình đơn, ngăn, quyền và phiên; xử lý yêu cầu trùng, phiên hết hạn, lệnh đã cấp nhưng khóa không mở, sự kiện đến sai thứ tự.
2. Làm Dashboard tối thiểu: danh sách đơn và trạng thái, trạng thái ngăn/cửa, sự kiện gần đây, cảnh báo. Thao tác quản trị phải có xác thực.
3. Bổ sung test cho chuyển trạng thái giao/nhận, thẻ sai, hai đơn tranh ngăn, phát lại `request_id`/`event_id` và sự kiện không thuộc phiên.
4. Nếu nhóm chọn CN07 cho demo, chỉ dùng cảm biến/camera ảo nếu mô hình Wokwi đáp ứng; nếu không thì ghi rõ ngoài phạm vi, không tuyên bố đã xác minh kiện.

**Đạt khi:** API và Dashboard chạy từ hướng dẫn; test tự động đạt; không thể cập nhật đơn chỉ bằng một sự kiện `door_closed` rời rạc.

## Người 3 — MQTT, tích hợp & kiểm thử

1. Chuẩn hóa hợp đồng bản tin giữa người 1 và 2: topic, `request_id`, `event_id`, `compartment_id`, kết quả ACK, timeout; ghi vào tài liệu trước khi thử trọn tuyến.
2. Kiểm chứng Broker Mosquitto 1883 cho Wokwi cục bộ; cấu hình TLS 8883/tài khoản/ACL là bài thử mở rộng phần mềm, không cần thiết bị tủ thật. Không đưa khóa riêng/mật khẩu lên repo.
3. Chạy bài smoke MQTT và Wokwi end-to-end; thử thẻ sai, khách quét trước khi giao, quét lặp, cửa không mở, cửa bỏ ngỏ, mất Broker và khởi động lại.
4. Thu ảnh/video/log, đo độ trễ, lập bảng KT01–KT12; KT13–KT14 chỉ khi có CN07. Cập nhật README, giới hạn và báo cáo cuối.

**Đạt khi:** người khác chạy lại demo Wokwi theo README và nhận cùng trạng thái; các bài TLS/ACL đã chọn được ghi kết quả hoặc đánh dấu chưa thử; báo cáo phân biệt số đo mô phỏng với mục tiêu thiết kế.

## Quy tắc phối hợp và thứ tự tích hợp

1. Cả ba nhánh bắt đầu từ cùng `main`; mỗi người chỉ sửa phạm vi sở hữu. Thay đổi MQTT payload hoặc schema ảnh hưởng người khác phải ghi trong Pull Request và trao đổi trước khi merge.
2. Mốc 1: người 1 và 2 cùng chốt hợp đồng MQTT với người 3. Mốc 2: hoàn tất demo Wokwi online. Mốc 3: hoàn thiện độ tin cậy mô phỏng, bảo mật phần mềm được chọn và báo cáo.
3. Merge PR Backend và Firmware sau khi test riêng đạt; người 3 cập nhật nhánh tích hợp theo `main` mới rồi chạy lại end-to-end trước khi merge PR tích hợp.
4. Chỉ đưa CN07 (cân/nhận dạng kiện) vào mục tiêu demo chính nếu mô phỏng Wokwi hỗ trợ đủ linh kiện và có bài thử KT13–KT14; nếu không, nêu rõ là hướng mở rộng ngoài demo hiện tại.
