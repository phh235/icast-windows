# <sub><img src="icast/icon.png" width="36" height="36" alt=""></sub> iCast

[English](README.md) · **Tiếng Việt** · [简体中文](README.zh-CN.md)

Phản chiếu màn hình và điều khiển iPhone trên Windows. Dự án do **phh235** duy trì.

iCast biến máy tính Windows 10 hoặc 11 thành bộ thu AirPlay. Bạn có thể xem màn hình và nghe âm thanh từ iPhone qua Wi‑Fi mà không cần cài ứng dụng lên điện thoại. Nếu muốn dùng chuột và bàn phím để điều khiển iPhone, hãy thiết lập thêm WebDriverAgent qua USB.

[Cài đặt](#cài-đặt) · [Cài bằng agent](#cài-bằng-agent) · [Phản chiếu màn hình](#phản-chiếu-màn-hình) · [Điều khiển qua USB](#điều-khiển-qua-usb) · [Xử lý sự cố](#xử-lý-sự-cố)

## Cài đặt

Bạn cần Windows 10 hoặc 11, Python 3.11 trở lên và iPhone kết nối cùng mạng Wi‑Fi với máy tính. Khi cài Python, chọn **Add python.exe to PATH**. [Tải Python](https://www.python.org/downloads/)

1. Trên GitHub, chọn **Code → Download ZIP** rồi giải nén vào thư mục bạn định giữ, ví dụ `C:\Apps\icast-windows`. Nếu dùng Git, chạy `git clone https://github.com/phh235/icast-windows.git`.
2. Mở thư mục dự án và chọn cách cài phù hợp.

   - **Phản chiếu và điều khiển qua USB.** Chạy `install.bat` để cài đầy đủ thư viện trên máy tính. Sau đó làm theo mục [Điều khiển qua USB](#điều-khiển-qua-usb).
   - **Chỉ phản chiếu hình ảnh và âm thanh.** Mở PowerShell trong thư mục dự án và chạy lệnh dưới đây để cài ít thư viện hơn. Nếu sau này cần điều khiển qua USB, chạy lại `install.bat`.

     ```powershell
     .\install.bat --mirror-only
     ```

Trình cài đặt tạo môi trường Python riêng tại `%LOCALAPPDATA%\iCast\venv` và thêm lối tắt **iCast** trên màn hình nền và trong menu Start. Để mở iCast từ PowerShell, chạy `.\run.bat` trong thư mục dự án. Nếu chuyển thư mục dự án sang chỗ khác, chạy lại `install.bat` để cập nhật lối tắt.

### Cài bằng agent

Dán yêu cầu dưới đây vào một agent có thể chạy lệnh trực tiếp trên máy Windows của bạn. Đổi chế độ thành `mirror-and-usb` nếu muốn dùng thêm chuột và bàn phím để điều khiển iPhone.

~~~text
Cài iCast trên máy Windows này ở chế độ mirror-only.
Lấy mã nguồn từ https://github.com/phh235/icast-windows. Nếu máy đã có bản sao của
repo này, dùng lại bản đó. Nếu chưa, chọn thư mục lưu lâu dài trong hồ sơ người dùng,
ví dụ %USERPROFILE%\Projects, rồi chạy lệnh dưới đây trong thư mục đó.
git clone https://github.com/phh235/icast-windows.git
Nếu máy chưa có Git, tải và giải nén tệp ZIP bên dưới.
https://github.com/phh235/icast-windows/archive/refs/heads/main.zip
Mở thư mục vừa lấy về, đọc README.md rồi chạy các bước sau trong thư mục đó.

1. Kiểm tra python.exe trên PATH có phải Python 3.11 trở lên không. Nếu thiếu,
   cài từ nguồn chính thức, cập nhật PATH cho terminal và kiểm tra lại.
   Dùng lại môi trường iCast nếu đã có và giữ cấu hình của tôi.
2. Tự chạy lệnh cài đặt tương ứng. Chuyển đầu vào từ NUL để trình cài
   kết thúc mà không phải chờ nhấn phím.
   cmd.exe /d /c "install.bat --mirror-only < NUL"
   cmd.exe /d /c "install.bat < NUL"
   Kiểm tra mã thoát, sau đó chạy -m pip check bằng Python tại
   %LOCALAPPDATA%\iCast\venv\Scripts\python.exe.
3. Mở iCast bằng .\run.bat. Kiểm tra cửa sổ hoặc dòng khởi động mới nhất
   trong %LOCALAPPDATA%\iCast\icast.log để xác nhận bộ thu đã sẵn sàng.
   Nếu Windows hỏi quyền mạng riêng tư, nhờ tôi xác nhận. Hướng dẫn tôi
   chọn bộ thu trong mục Phản chiếu màn hình trên iPhone để thử kết nối.
4. Với mirror-and-usb, cài các thành phần còn thiếu theo liên kết trong
   README. Nhờ tôi xác nhận nếu Windows yêu cầu quyền quản trị viên.
   Chạy .\control.bat doctor và xử lý mục lỗi đầu tiên. Nếu cần cài hoặc ký lại WDA,
   chạy .\control.bat get-wda và đưa tôi đường dẫn IPA chính xác.
   Nhờ tôi cắm cáp, mở khóa, chọn Tin cậy, ký IPA trong Sideloadly hoặc
   bật Chế độ nhà phát triển khi cần. Chạy .\control.bat reveal-dev-mode
   nếu chưa thấy công tắc. Sau khi tôi xác nhận, chạy lại doctor. Khi
   đã phản chiếu được hình ảnh, bật USB bằng Ctrl+U và kiểm tra nút chuyển xanh lá.
5. Tự làm các bước có thể chạy từ terminal. Khi cần tôi thao tác, hãy
   nói rõ cần làm gì, chờ tôi xác nhận rồi tiếp tục. Cuối cùng báo
   những gì đã cài, đã kiểm tra và điểm nào còn vướng.
~~~

Nhập mật khẩu Tài khoản Apple, mã xác minh và mật mã iPhone trực tiếp trong Sideloadly hoặc trên điện thoại. [Chi tiết thiết lập USB](#điều-khiển-qua-usb)

## Phản chiếu màn hình

1. Mở **iCast**. Khi sẵn sàng, cửa sổ hiện **Waiting for iPhone**.
2. Nếu Tường lửa Windows hỏi quyền truy cập mạng cho Python, chọn **Mạng riêng tư (Private networks)** rồi cho phép truy cập. Trong cài đặt mạng Windows, đặt cấu hình của mạng Wi‑Fi đang dùng thành **Riêng tư (Private)**.
3. Trên iPhone, mở **Trung tâm điều khiển → Phản chiếu màn hình** và chọn tên máy tính. Bạn có thể đổi tên hiển thị bằng cách nhấp chuột phải trong iCast và chọn **Rename PC...**. [Hướng dẫn AirPlay của Apple](https://support.apple.com/vi-vn/102661)

Cửa sổ iCast không có viền, mặc định nằm trên các cửa sổ khác và ghi nhớ vị trí. Khi iPhone chuyển sang màn hình ngang, cửa sổ tự đổi theo. Mở iCast lần nữa sẽ đưa cửa sổ đang chạy lên trước.

### Cửa sổ và phím tắt

Rê chuột lên cửa sổ để hiện thanh điều khiển. Thanh này hiển thị tên thiết bị, tốc độ khung hình và các nút điều khiển USB, Home, ghim cửa sổ, thu nhỏ và đóng. Tên nút và mục menu được giữ bằng tiếng Anh để bạn dễ tìm trong ứng dụng.

| Thao tác | Kết quả |
|---|---|
| Kéo cửa sổ khi tắt điều khiển USB | Di chuyển cửa sổ |
| Nhấp đúp khi tắt điều khiển USB | Đổi giữa kích thước lớn và thông thường |
| `Ctrl` + cuộn chuột hoặc `Ctrl` + `+` / `-` | Đổi kích thước cửa sổ |
| `Ctrl` + `M` | Tắt hoặc bật âm thanh trên máy tính |
| `Ctrl` + `U` | Bật hoặc tắt điều khiển qua USB |
| `Ctrl` + `V` | Dán văn bản từ clipboard Windows vào ô đang chọn trên iPhone khi điều khiển USB đã sẵn sàng |
| `F11` / `Esc` | Bật tắt toàn màn hình / trở lại cửa sổ |
| `Ctrl` + `1` | Bật hoặc tắt Actual Pixels (1:1) |
| `Ctrl` + `Shift` + `C` | Chép khung hình hiện tại ở độ phân giải gốc |
| `Ctrl` + `H` | Về Màn hình chính khi điều khiển USB đã sẵn sàng |
| `Ctrl` + `J` | Mở Bộ chuyển đổi ứng dụng trên iPhone có nút Home khi điều khiển USB đã sẵn sàng |
| `Ctrl` + `↑` / `↓` | Tăng hoặc giảm âm lượng iPhone khi điều khiển USB đã sẵn sàng |

Với iPhone dùng Face ID, bạn có thể mở Bộ chuyển đổi ứng dụng bằng cử chỉ từ cạnh dưới trên điện thoại hoặc qua AssistiveTouch. iCast hiện gợi ý AssistiveTouch khi bạn nhấn `Ctrl` + `J` trên các mẫu này. [Hướng dẫn AssistiveTouch của Apple](https://support.apple.com/vi-vn/111794)

Nhấp chuột phải để mở menu. **Always on Top** ghim cửa sổ, **Size** chọn kích thước, **Sound** bật hoặc tắt âm thanh, **Save Screenshot** lưu ảnh PNG vào `Pictures\iCast`, **Open Settings Folder** mở thư mục chứa `settings.json`, **Open Log** mở nhật ký và **Quit** thoát ứng dụng.

**Stream Info...** hiện độ phân giải thực nhận, kích thước hiển thị và FPS. **Connection Diagnostics...** hiện trạng thái AirPlay và chạy phần kiểm tra USB.

**Full Screen** phóng hình vừa màn hình hiện tại và giữ tỷ lệ iPhone. **Copy Screenshot** chép ảnh gốc vào clipboard Windows.

**Actual Pixels (1:1)** hiển thị mỗi pixel nguồn bằng một pixel vật lý trên màn hình, kể cả khi Windows dùng scale 125% hoặc 150%. Nếu ảnh gốc quá lớn, **Stream Info...** sẽ báo iCast đang dùng chế độ vừa khung.

## Điều khiển qua USB

Để gửi thao tác chạm, vuốt và nhập chữ từ Windows, iCast dùng WebDriverAgent (WDA) chạy trên iPhone. Bạn cần cáp USB, bản iTunes từ trang Apple để có trình điều khiển USB phù hợp với Sideloadly và một Tài khoản Apple (Apple ID). Tài khoản miễn phí cũng dùng được. [Tải iTunes](https://www.apple.com/itunes/) · [WebDriverAgent](https://github.com/appium/WebDriverAgent)

### Thiết lập lần đầu

Mở PowerShell trong thư mục dự án. Các lệnh `control.bat` tự dùng môi trường Python của iCast.

1. **Kết nối iPhone.** Cài iTunes, cắm cáp, mở khóa iPhone và chọn **Tin cậy** khi máy hỏi có tin cậy máy tính này hay không.
2. **Tạo tệp cài đặt WDA.** Chạy lệnh sau. Tệp `WebDriverAgentRunner.ipa` được lưu trong thư mục Downloads.

   ```powershell
   .\control.bat get-wda
   ```

   iCast tải phiên bản WDA đã được chỉ định, kiểm tra mã băm SHA‑256 và bỏ các gói ký hiệu gỡ lỗi `.dSYM` có thể làm Sideloadly báo lỗi khi ký. Tệp IPA giữ lại XCTest runner và các framework cần thiết, nên bạn có thể đưa thẳng tệp này vào Sideloadly. [Bản phát hành WDA](https://github.com/appium/WebDriverAgent/releases/tag/v16.15.3)
3. **Ký và cài WDA.** Mở Sideloadly, chọn iPhone và tệp IPA, nhập Tài khoản Apple rồi nhấn **Start**. Việc ký vẫn phụ thuộc vào bản WDA, Sideloadly và yêu cầu ký của Apple. Nếu dùng tài khoản miễn phí, hãy cài lại bằng Sideloadly sau 7 ngày khi chữ ký hết hạn. [Tải Sideloadly](https://sideloadly.io)
4. **Tin cậy hồ sơ nhà phát triển.** Trên iPhone, vào **Cài đặt → Cài đặt chung → VPN & Quản lý thiết bị**, chọn tài khoản dùng để ký rồi xác nhận theo hướng dẫn trên màn hình.
5. **Bật Chế độ nhà phát triển trên iOS 16 trở lên.** Vào **Cài đặt → Quyền riêng tư & Bảo mật → Chế độ nhà phát triển**. Bật chế độ này, khởi động lại iPhone rồi mở khóa và xác nhận lần nữa. Nếu chưa thấy mục này trong Cài đặt, chạy lệnh sau.

   ```powershell
   .\control.bat reveal-dev-mode
   ```

6. **Kiểm tra thiết lập.** Chạy lệnh dưới đây và làm theo hướng dẫn nếu có mục báo lỗi.

   ```powershell
   .\control.bat doctor
   ```

7. **Bắt đầu điều khiển.** Phản chiếu đúng chiếc iPhone đang cắm USB. Nhấn `Ctrl` + `U`, bấm biểu tượng bàn tay hoặc chọn **Control iPhone (USB)** trong menu chuột phải. Nút nhấp nháy màu cam khi đang khởi động và chuyển xanh lá khi sẵn sàng. Lần đầu có thể lâu hơn vì iCast cần tải và gắn Developer Disk Image.

Giữ cáp USB trong khi điều khiển. iCast kiểm tra iPhone kết nối USB có đúng là thiết bị đang phản chiếu hay không. Khi bạn dừng hoặc đổi phiên phản chiếu, phiên điều khiển cũng kết thúc.

iOS 17.0–17.3 cần quyền quản trị viên để chạy `pymobiledevice3 remote tunneld` trước khi bật điều khiển. Trên iOS 17.4 trở lên và iOS 16 trở xuống, iCast có thể khởi động WDA mà không cần quyền này.

### Thao tác khi đã kết nối

| Thao tác trong vùng màn hình iPhone | Kết quả |
|---|---|
| Nhấp / nhấp đúp | Chạm / chạm hai lần |
| Nhấn giữ từ 0,5 giây, không di chuột | Chạm và giữ |
| Kéo rồi thả | Vuốt từ điểm bắt đầu đến điểm thả chuột |
| Cuộn chuột | Cuộn nội dung |
| Gõ phím | Nhập chữ, hỗ trợ Enter và Backspace |
| Kéo thanh trên hoặc giữ `Alt` rồi kéo | Di chuyển cửa sổ iCast |

### Lệnh hỗ trợ

Chạy `.\control.bat COMMAND` và thêm `--help` để xem tham số của từng lệnh.

| Lệnh | Công dụng |
|---|---|
| `doctor` | Kiểm tra điều kiện điều khiển qua USB. Thêm `--ip ADDRESS` để kiểm tra một WDA đang chạy qua Wi‑Fi |
| `reveal-dev-mode` | Hiện công tắc Chế độ nhà phát triển trong Cài đặt |
| `get-wda` | Tạo `WebDriverAgentRunner.ipa`. Dùng `--out PATH` để chọn nơi lưu |
| `start` | Khởi động WDA qua USB từ dòng lệnh và giữ phiên chạy đến khi nhấn `Ctrl` + `C` |
| `tap X Y` | Chạm tại tọa độ X, Y tính theo điểm trên iPhone |
| `home` | Về Màn hình chính |
| `type TEXT` | Nhập chữ vào ô đang được chọn. `\n` là Enter và `\b` là Backspace |

Các lệnh `tap`, `home` và `type` mặc định dùng `http://127.0.0.1:8100`. Nếu đã chạy `start`, hãy truyền địa chỉ mà lệnh đó in ra bằng tham số `--url`.

## Xử lý sự cố

| Hiện tượng | Cách xử lý |
|---|---|
| Không thấy máy tính trong danh sách Phản chiếu màn hình | Kiểm tra hai thiết bị có cùng mạng Wi‑Fi, mạng Windows đang ở chế độ **Riêng tư** và tường lửa cho phép `pythonw.exe` / `python.exe` trên mạng riêng tư. Mạng khách, chế độ cô lập thiết bị trên bộ định tuyến hoặc VPN có thể cản trở việc phát hiện. Mở lại iCast để công bố lại bộ thu AirPlay. |
| **Could not decrypt the video** | Dừng phản chiếu trên iPhone rồi kết nối lại. Nếu lỗi lặp lại, chọn **Open Log** và đính kèm nhật ký khi báo lỗi. |
| **iPhone is locked** | Mở khóa iPhone để tiếp tục phản chiếu. |
| Không có âm thanh | Kiểm tra mục **Sound** hoặc nhấn `Ctrl` + `M`. Xem lại thiết bị phát âm thanh trong Windows và mức âm lượng trên iPhone. |
| Không bật được điều khiển | Chạy `.\control.bat doctor`, mở khóa iPhone và kiểm tra hồ sơ nhà phát triển đã được tin cậy. Nếu chữ ký hết hạn, cài lại IPA bằng Sideloadly. |
| iCast báo sai phiên bản WDA | Xem bundle ID và phiên bản trong thông báo. Nếu bundle được chọn là bản cũ, gỡ bản đó rồi thử lại với bản tạo từ `get-wda`. |

Nhật ký nằm trong `%LOCALAPPDATA%\iCast`. Xem `icast.log` cho ứng dụng, `wda_runner.log` cho WDA và `wda_forward.log` cho phiên chuyển tiếp cổng USB đã kết thúc gần nhất.

## Cài đặt và giới hạn

Trong menu chuột phải, chọn **Resolution** (720p, 1080p hoặc 1440p) và **Frame Rate** (30, 60 hoặc 120 FPS thử nghiệm) riêng biệt. Kết nối lại Screen Mirroring để gửi yêu cầu mới. **Stream Info...** hiện độ phân giải nhận được và FPS hiển thị, tùy vào luồng iPhone gửi.

Tệp `%LOCALAPPDATA%\iCast\settings.json` được tạo lần đầu khi mở iCast. Thoát ứng dụng trước khi sửa tệp.

| Khóa | Mặc định | Ý nghĩa |
|---|---|---|
| `name` | Tên máy tính | Tên hiện trong danh sách Phản chiếu màn hình |
| `width`, `height` | `1920`, `1080` | Kích thước luồng hình mà iCast yêu cầu từ iPhone |
| `fps` | `60` | Tốc độ khung hình yêu cầu |
| `audio` | `true` | Phát âm thanh iPhone trên máy tính |
| `always_on_top` | `true` | Giữ cửa sổ iCast trên các cửa sổ khác |
| `phone_height` | `0` (tự động) | Cạnh dài của vùng hiển thị iPhone, tính theo pixel logic của Windows |

- **Độ nét.** Bạn có thể thử `"height": 1440`. Đây là mức iCast yêu cầu. Độ nét thực tế phụ thuộc vào luồng iPhone gửi và kích thước hiển thị. Nếu iPhone không kết nối được, đổi lại `1080`.
- **Nội dung có DRM.** Một số ứng dụng như Netflix hoặc Apple TV có thể hiện màn hình đen khi phản chiếu.
- **Độ trễ điều khiển.** Lệnh WDA thường mất khoảng 100–200 ms. Khi nhấp một lần, iCast chờ thêm khoảng 0,2 giây để phân biệt với nhấp đúp. Thao tác vuốt được gửi lúc thả chuột, nên không thể kéo vật thể theo con trỏ hoặc vẽ trực tiếp.

Tệp cấu hình cũng lưu vị trí cửa sổ và danh tính AirPlay. Xóa tệp sẽ đặt lại toàn bộ cấu hình. Sau đó, iPhone sẽ thấy iCast như một bộ thu mới.

## Dành cho nhà phát triển

iCast dùng mDNS để xuất hiện trong danh sách AirPlay và RTSP để thiết lập phiên. PyAV/FFmpeg giải mã video H.264 và âm thanh AAC-ELD. Giao diện dùng PySide6 và âm thanh được phát qua sounddevice. Khi điều khiển qua USB, pymobiledevice3 khởi chạy WDA và chuyển tiếp yêu cầu HTTP qua cổng loopback.

Chạy các lệnh từ thư mục dự án. Bạn có thể dùng Python tại `%LOCALAPPDATA%\iCast\venv\Scripts\python.exe` hoặc môi trường Python 3.11+ đã cài `requirements.txt`. Dùng `requirements-control.txt` cho phần kiểm tra pymobiledevice3. Công cụ mô phỏng iPhone cần thêm NumPy.

```powershell
python tests/test_fairplay.py
python -m unittest discover -s tests -q
```

Để thử phản chiếu mà không cần iPhone, mở hai cửa sổ PowerShell. Chạy lệnh thứ nhất ở cửa sổ đầu, rồi chạy lệnh thứ hai ở cửa sổ còn lại.

```powershell
python -m icast --local
python tools/fake_iphone.py 127.0.0.1 7100
```

`--local` chỉ lắng nghe trên máy tính này, dùng tệp cấu hình riêng và không phát thông báo mDNS. Các tùy chọn khác gồm `--port N` để đổi cổng RTSP, `--control` để bật điều khiển USB khi bắt đầu phản chiếu, `--grab DIR` để lưu ảnh PNG mỗi 2 giây và `--quit-after N` để tự thoát sau N giây.

| Đường dẫn | Nội dung |
|---|---|
| `icast/airplay.py`, `icast/discovery.py` | Phiên RTSP và phát hiện AirPlay qua mDNS |
| `icast/pairing.py`, `icast/fairplay/` | Ghép nối AirPlay và xử lý FairPlay |
| `icast/video.py`, `icast/audio.py` | Giải mã và phát hình ảnh, âm thanh |
| `icast/ui.py`, `icast/config.py` | Cửa sổ iCast và cấu hình |
| `icast/control/` | Điều khiển qua USB, WDA và đóng gói IPA |
| `tests/`, `tools/` | Kiểm thử, mô phỏng iPhone và công cụ tạo lại mã FairPlay |

Để tạo lại `_tables.py` và `_garble.py` từ mã playfair trong UxPlay, chạy `python tools/gen_fairplay.py path\to\UxPlay\lib`.

## Ghi công và giấy phép

iCast tham khảo UxPlay, RPiPlay và shairplay. Các thư viện và nguồn dữ liệu kiểm thử được liệt kê trong [bản tiếng Anh](README.md#credits).

Toàn bộ ứng dụng được phân phối theo [GPL-3.0](LICENSE). Mô-đun `icast/fairplay/` chuyển thể từ mã [playfair](licenses/playfair-LICENSE.md). Thông tin bản quyền và các thay đổi của phiên bản này có trong [NOTICE](NOTICE).

iCast là dự án độc lập, không liên kết với Apple Inc. AirPlay, iPhone, FairPlay và iOS là nhãn hiệu của Apple Inc. Chỉ phản chiếu thiết bị và nội dung mà bạn được phép sử dụng.
