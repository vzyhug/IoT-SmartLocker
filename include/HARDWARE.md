# Linh kiện ảo Wokwi — Smart Locker N11

Project hiện tại dùng hoàn toàn linh kiện ảo Wokwi; bảng dưới là wiring của `diagram.json`, không phải BOM mua sắm hay yêu cầu phần cứng thật.

| Linh kiện ảo | Vai trò mô phỏng | Kết nối ESP32 |
| --- | --- | --- |
| ESP32 DevKit C V4 | Firmware và MQTT client | Board `esp` trong `diagram.json` |
| MFRC522 + Red Card `AABBCCDD`, Green Card `11223344` | RFID shipper/khách; quét thẻ kiện bổ sung | SCK 18, MISO 19, MOSI 23, SDA/SS 5, RST 27; 3V3/GND |
| Slide switch `Door` | Reed switch cửa | GPIO32; đóng = LOW, mở = HIGH |
| LED vàng `lock` | Xung mở khóa tượng trưng | GPIO26; HIGH khoảng 500 ms |
| LED xanh `ok` | Cấp quyền/phiên đang hoạt động | GPIO25 |
| LED đỏ `error` | Từ chối hoặc cảnh báo | GPIO33 |
| Buzzer | Báo thao tác và cảnh báo | GPIO4 |
| PIR motion sensor | Hiện diện để bật đèn ảo | OUT GPIO34; VCC 3V3/GND |
| LED trắng `light` | Đèn chiếu sáng ảo theo PIR | GPIO14 |
| Gas sensor digital output | Cảnh báo khói/gas mô phỏng | DOUT GPIO35; VCC 5V/GND; firmware báo khi DOUT LOW |
| LCD1602 I2C | Hướng dẫn và trạng thái | SDA 21, SCL 22; địa chỉ `0x27`; VCC 3V3/GND |
| HX711 5 kg | Bằng chứng thay đổi khối lượng | DT GPIO16, SCK GPIO17; VCC 5V/GND; hệ số demo 420 |

## Thao tác trong simulator

- Door phải đóng trước khi quét thẻ. Sau `Access granted`, LED vàng phát xung, LED xanh sáng; mở rồi đóng switch để sinh `door_open`/`door_closed`. Mở khi chưa có phiên hợp lệ tạo `door_forced`, LED đỏ và còi.
- MFRC522 panel cho chọn thẻ ảo. Wokwi hiển thị UID có dấu `:`, firmware chuẩn hóa thành chuỗi hex không dấu.
- Khi đơn cần xác minh kiện, quét thẻ kiện khi Door đang mở; điều chỉnh load trên HX711 ảo rồi đóng Door. Serial và event ghi UID kiện cùng khối lượng trước/sau.
- PIR bật LED trắng tối đa 30 giây sau chuyển động; gas sensor digital tạo cảnh báo khi DOUT xuống LOW.
- OTP/QR trong demo được nhập qua Wokwi Serial Monitor (`OTP:<6 số>` hoặc `QR:<token>`); đây là đầu vào phần mềm ảo, không phải keypad hoặc camera.

LED vàng chỉ biểu diễn xung mở khóa; switch chỉ biểu diễn trạng thái cửa. Không có thiết bị vật lý trong phạm vi dự án.
