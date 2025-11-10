pyinstaller \
  --noconfirm \
  --onedir \
  --windowed \
  --name "NewsBoard" \
  --icon "/home/nicolas/Apps/NewsBoard/resources/icon.ico" \
  --add-data "/home/nicolas/Apps/NewsBoard/resources:resources" \
  --collect-all PyQt6 \
  --collect-all yt_dlp \
  --collect-submodules PyQt6.QtMultimedia \
  --collect-submodules PyQt6.QtMultimediaWidgets \
  "/home/nicolas/Apps/NewsBoard/main.py"
