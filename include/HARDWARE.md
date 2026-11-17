# Phần cứng người 1 — một ngăn A01

Firmware hiện nhắm tới **ESP32 DevKit V1 / `esp32dev`**. Bảng này là thiết kế đấu dây cho một ngăn, chưa phải kết quả đo trên linh kiện của nhóm. Kiểm tra nhãn chân trên bo và datasheet module thực tế trước khi cấp điện.

## BOM tối thiểu

| Thành phần | Số lượng | Yêu cầu chọn |
| --- | ---: | --- |
| ESP32 DevKit V1 | 1 | Wi-Fi 2,4 GHz, GPIO theo bảng dưới |
| MFRC522 và thẻ RFID | 1 bộ | Cấp 3,3 V, SPI; UID chỉ dùng cho demo |
| Reed switch + nam châm | 1 | Đóng cửa kéo GPIO32 xuống GND |
| Khóa chốt điện 12 V | 1 | Chốt loại/dòng định mức trước khi chọn nguồn và driver |
| MOSFET N logic-level hoặc module relay | 1 | Chịu được dòng khởi động và điện áp của khóa; đầu vào điều khiển phù hợp mức 3,3 V |
| Diode dập xung cuộn khóa | 1 | Chọn theo dòng/điện áp khóa; đặt sát cuộn dây |
| Adapter 12 V + cầu chì phù hợp | 1 | Dòng định mức lớn hơn dòng khóa đo được và các tải liên quan |
| Nguồn ổn áp cho ESP32 | 1 | USB riêng khi thử bàn; nếu dùng buck, chỉnh đúng điện áp đầu vào cho bo thực tế |
| LED xanh, LED đỏ, điện trở hạn dòng | 2 bộ | LED báo cấp quyền và lỗi/cảnh báo |
| Dây, terminal, hộp che mạch | Theo nhu cầu | Tránh dây khóa 12 V chạm mạch logic |

## GPIO

| GPIO ESP32 | Nối tới | Chức năng |
| --- | --- | --- |
| 18 / 19 / 23 | MFRC522 SCK / MISO / MOSI | SPI |
| 5 / 27 | MFRC522 SDA(SS) / RST | Chọn chip và reset |
| 32 | Reed switch → GND khi cửa đóng | `INPUT_PULLUP`; HIGH = cửa mở |
| 26 | Đầu vào driver khóa | HIGH tạo xung 500 ms, LOW tắt |
| 25 / 33 | LED xanh / LED đỏ qua điện trở | Trạng thái và cảnh báo |

MFRC522 VCC nối **3,3 V**, GND nối GND. Đối với driver MOSFET low-side: cực dương khóa nối +12 V; cực âm khóa nối drain; source nối GND; GPIO26 đi qua điện trở gate thích hợp và có điện trở kéo gate xuống GND. Diode mắc song song cuộn khóa với **cathode về +12 V**, anode về phía drain. Nối chung GND nguồn 12 V và ESP32 tại điểm phù hợp. Nếu dùng relay/module khác, kiểm tra cực điều khiển và mức kích hoạt; firmware giả định HIGH = mở khóa. Không đưa 12 V vào GPIO/3V3.

## Trình tự thử trên bàn

1. **Chưa lắp khóa:** đo nguồn ESP32 và MFRC522, kiểm tra RFID và công tắc cửa. Cửa đóng phải đọc LOW, cửa mở HIGH; nếu ngược, sửa đấu dây hoặc cấu hình firmware.
2. Gắn LED thay driver khóa, thử thẻ đúng/sai và xác nhận GPIO26 chỉ HIGH khoảng 500 ms. Reset ESP32 trong lúc mở thử; đầu ra phải trở về LOW.
3. Gắn driver và khóa với nguồn 12 V riêng; đo điện áp, dòng thường và dòng khởi động. Quan sát nóng, sụt nguồn, ESP32 reset và chiều diode. Chỉ gắn vào cửa sau khi các phép thử này đạt.
4. Thử cửa mở/đóng, bỏ ngỏ, mở ngoài phiên, mất Wi-Fi/Broker và mất điện. Ghi log, số lần lặp, thông số nguồn và video. Nếu nguồn khóa có thể làm ESP32 reset hoặc chốt mở ngoài ý muốn, dừng thử và sửa mạch nguồn/driver.

Đối với MQTT/TLS trên bo thật, mạng phải cho ESP32 truy cập DNS và NTP được chỉ định trong `secrets.h`; firmware chỉ kết nối Broker sau khi có giờ đáng tin cậy để kiểm tra chứng chỉ. Nếu tủ đặt trong VLAN riêng, nhóm mạng cần cấp luồng này theo chính sách ở tài liệu tổng hợp.

## Giới hạn

- Reed switch xác nhận trạng thái cửa, không xác nhận chốt đã gài hay có kiện hàng.
- `esp_timer` tắt GPIO sau xung; với khóa thật cần bổ sung giới hạn thời gian bằng phần cứng/driver phù hợp nếu hỏng phần mềm có thể gây nguy hiểm hoặc quá nhiệt.
- Chế độ Wokwi thay khóa bằng LED vàng. Nó kiểm tra logic GPIO, không chứng minh khóa 12 V hoạt động thật.
