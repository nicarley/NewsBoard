python -m PyInstaller ^
  --noconfirm ^
  --onedir ^
  --windowed ^
  --name "NewsBoard" ^
  --icon "C:\Users\admin7\OneDrive - Americana Building Products\Projects\NewsBoard\resources\icon.ico" ^
  --add-data "C:\Users\admin7\OneDrive - Americana Building Products\Projects\NewsBoard\resources;resources" ^
  --collect-all PyQt6 ^
  --collect-all yt_dlp ^
  --collect-submodules PyQt6.QtMultimedia ^
  --collect-submodules PyQt6.QtMultimediaWidgets ^
  "C:\Users\admin7\OneDrive - Americana Building Products\Projects\NewsBoard\main.py"
