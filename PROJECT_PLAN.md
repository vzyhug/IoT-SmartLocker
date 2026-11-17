# Kế hoạch hoàn thiện Smart Locker N11

## 1. Đã có và đã kiểm chứng

- [x] Tài liệu yêu cầu và kiến trúc tổng hợp.
- [x] Firmware một ngăn `A01`: RFID, Reed switch, xung mở khóa, LED, yêu cầu xác thực qua MQTT.
- [x] Backend một ngăn: tạo/xem đơn, quyền shipper/khách theo UID, chuyển trạng thái sau sự kiện cửa, loại trùng `event_id`.
- [x] Cấu hình mẫu Mosquitto TLS/ACL cho triển khai thật và chế độ Wokwi dùng Broker localhost.
- [x] Sơ đồ Wokwi, cấu hình PlatformIO và bản build firmware Wokwi thành công.
- [x] Unit test luồng giao–nhận Backend.
- [x] Smoke test qua Broker Mosquitto localhost: xác thực thẻ, sự kiện cửa và chuyển trạng thái đơn.

Các dấu kiểm trên xác nhận mã/cấu hình đã được tạo hoặc test như nêu, **không đồng nghĩa hệ thống đã chạy trọn tuyến trên tủ thật**.

## 2. Hoàn thiện demo mô phỏng — ưu tiên ngay

- [ ] Chạy Wokwi for VS Code cùng Private IoT Gateway; xác nhận ESP32 kết nối tới Mosquitto localhost.
- [ ] Thực hiện trọn kịch bản thẻ shipper → cửa mở/đóng → `waiting_pickup` → thẻ khách → `received`; lưu log và ảnh/video.
- [ ] Thử thẻ sai, thẻ khách quét trước khi giao, quét lặp, không mở cửa sau khi được cấp quyền, cửa mở ngoài phiên và cửa bỏ ngỏ.
- [ ] Kiểm tra thực tế các chân/UID trong Wokwi và chỉnh `diagram.json` nếu Simulator báo lỗi.

**Tiêu chí hoàn thành:** toàn bộ kịch bản có log MQTT, trạng thái đơn đúng và không mở khi không có quyền.

## 3. Hoàn thiện bản tủ vật lý

- [ ] Chốt bo ESP32, một hay nhiều ngăn, khóa 12 V, dòng nguồn, MOSFET/relay, diode bảo vệ, Reed switch và bảng GPIO.
- [ ] Chế tạo/lắp ngăn; đo điện áp và dòng khóa; xác nhận khóa an toàn khi khởi động, reset, mất điện và mất Wi-Fi.
- [ ] Cấp chứng chỉ CA, tài khoản và ACL riêng từng tủ; bật MQTT/TLS 8883; bỏ chế độ MQTT 1883 của mô phỏng.
- [ ] Thử trọn tuyến với khóa và cảm biến thật; đo thời gian phản hồi theo PCN01 và lưu kết quả.

**Tiêu chí hoàn thành:** bản thử vật lý mở đúng ngăn, không mở ngoài quyền và có kết quả đo điện/độ trễ.

## 4. Nghiệp vụ và độ tin cậy còn thiếu

- [ ] Thêm timeout và xử lý lỗi theo phiên ở Backend; xác nhận lệnh mở đã thực thi trước khi tiêu thụ quyền.
- [ ] Thêm hàng đợi sự kiện bền vững trên ESP32 và ACK tầng ứng dụng từ Backend trước khi xóa; kiểm tra mất mạng/reset (CN06).
- [ ] Bổ sung Dashboard: đơn, ngăn, cửa, thiết bị, log, cảnh báo và thao tác quản trị có phân quyền.
- [ ] Chốt phương thức người dùng cho bản đầy đủ: OTP/QR cần keypad/camera hoặc ứng dụng tương ứng; RFID UID chỉ phù hợp demo.
- [ ] Chốt chính sách thời gian, hạn quyền, giới hạn thử sai, cảnh báo cửa bất thường, quyền bảo trì và audit trail.

**Tiêu chí hoàn thành:** các tình huống mất mạng, lặp bản tin, reset và truy cập sai được xử lý đúng theo KT01–KT12 trong tài liệu.

## 5. Xác minh kiện hàng (CN07, nếu nhóm chọn phạm vi này)

- [ ] Mức 2: gắn load cell/HX711 hoặc cảm biến hiện diện từng ngăn; hiệu chuẩn, đo trước/sau và thử với kiện nhẹ nhất.
- [ ] Mức 3: cấp `package_id` duy nhất, quét mã kiện và ghi bằng chứng mã nằm trên kiện trong ngăn; đối chiếu ảnh/nhãn, xử lý trường hợp không đọc được.
- [ ] Chạy KT13–KT14 gồm quét mã đúng nhưng đặt kiện khác. Không tuyên bố xác minh được nội dung bên trong kiện nếu chỉ có mã nhãn/cân.

**Tiêu chí hoàn thành:** báo cáo nêu rõ mức xác minh đạt được và có bằng chứng cho từng kết luận.

## 6. Báo cáo và bàn giao

- [ ] Ghi môi trường, linh kiện, phiên bản firmware/Backend, sơ đồ nối dây và cấu hình Broker đã ẩn bí mật.
- [ ] Lưu bảng kết quả KT01–KT14 theo phạm vi đã triển khai; tách mục tiêu thiết kế với số đo thực nghiệm.
- [ ] Quay demo Wokwi và tủ thật; cập nhật README thành hướng dẫn tái hiện; chốt các giới hạn còn lại.
