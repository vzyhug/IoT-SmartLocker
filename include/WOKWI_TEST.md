# Kiểm thử phần người 1 trên Wokwi

## Chuẩn bị

1. Build: `C:\Users\duyhu\.platformio\penv\Scripts\pio.exe run -e wokwi` (hoặc `pio run -e wokwi` khi PlatformIO có trên PATH).
2. Chạy Mosquitto localhost 1883, `backend/mqtt_worker.py` với `MQTT_TLS=0`, và API theo README. Tạo đơn `A01`: thẻ shipper đỏ `AABBCCDD`, thẻ khách xanh lá `11223344`.
3. Wokwi for VS Code cần license đã kích hoạt và Private IoT Gateway để `host.wokwi.internal` tới Broker trên máy. Mở `diagram.json`, chọn **Wokwi: Start Simulator**.

## Kịch bản

| Bước | Thao tác | Kết quả mong đợi |
| --- | --- | --- |
| 1 | Để công tắc Door ở vị trí đóng (GPIO32 nối GND) | Serial có `MQTT connected`; LED khóa và LED xanh tắt |
| 2 | Quét thẻ xanh lá trước khi giao | `Authorization denied`; LED vàng không sáng; đơn vẫn `waiting_deposit` |
| 3 | Quét thẻ đỏ | `Authorization requested` rồi `Access granted`; LED vàng sáng khoảng 500 ms, LED xanh sáng |
| 4 | Gạt Door mở rồi đóng | Sự kiện `door_open`, `door_closed`; LED xanh tắt; đơn thành `waiting_pickup` |
| 5 | Quét thẻ đỏ lần nữa | Bị từ chối, không phát xung mở |
| 6 | Quét thẻ xanh lá, mở rồi đóng cửa | Đơn thành `received` |
| 7 | Gạt Door mở khi không có phiên | Sự kiện `door_forced`, LED đỏ bật; khi đóng LED đỏ tắt |
| 8 | Cấp quyền nhưng không gạt cửa trong 30 giây | Firmware về Idle; khóa vẫn tắt; Backend hiện có thể giữ phiên thêm tới 60 giây |
| 9 | Cấp quyền, mở cửa quá 30 giây | Sự kiện `door_left_open` một lần; LED đỏ bật |

Thử reset mô phỏng sau mỗi bước. `event_id` dùng bộ đếm trong NVS và không được lặp sau reset. Nếu Wokwi xóa toàn bộ flash khi khởi động lại một simulation mới, đó là một thiết bị mới trong mô phỏng và không thay thế bài thử reset trên bo thật.

## Ghi bằng chứng

Lưu ảnh sơ đồ Wokwi, Serial Monitor (ẩn thông tin nhạy cảm), MQTT log, trạng thái đơn trước/sau và video các bước 2–7. Ghi phiên bản build, ngày giờ, số lần lặp và kết quả thực tế. Chưa được đánh dấu đạt khi chưa trực tiếp chạy các bước này trong Wokwi.
