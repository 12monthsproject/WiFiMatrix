#!/bin/zsh
# =============================================================
# WiFiMatrix.app 一键打包脚本（macOS，本机运行）
# 产出：dist/WiFiMatrix.app + WiFiMatrix.dmg（未签名，自用零障碍）
# 说明：
#   · 全程在你的 Mac 本机执行（沙箱无法跨平台打 macOS 包）
#   · 未签名 DMG：本机/U盘/git 分发不弹警告；网上下载需「右键→打开」一次
#   · 留有正式签名钩子（见文末注释），日后有 Developer ID 可直接补
# =============================================================
set -e

APP_NAME="WiFiMatrix"
APP_VERSION="1.3.0"
SCRIPT_SRC="/Users/luke/vibecoding/wifi/wifi_monitor.py"
PROJ_DIR="/Users/luke/vibecoding/wifi/WiFiMatrixApp"
cd "$PROJ_DIR"

# WorkBuddy/安全中心可能拦截系统临时目录的文件操作，重定向到工程内 tmp
mkdir -p "$PROJ_DIR/.tmp"
export TMPDIR="$PROJ_DIR/.tmp"

echo "==> [1/6] 环境检查"
[[ "$(uname)" == "Darwin" ]] || { echo "错误：请在 macOS 上运行"; exit 1; }
command -v python3 >/dev/null || { echo "错误：未找到 python3"; exit 1; }
python3 --version

echo "==> [2/6] 构建环境（venv）"
[[ -d .venv ]] || python3 -m venv .venv
if ! ./.venv/bin/python -c "import webview, PyInstaller" 2>/dev/null; then
  echo "首次构建：安装依赖（pywebview / pyinstaller）…"
  ./.venv/bin/pip install -q --upgrade pip
  ./.venv/bin/pip install -q pywebview pyinstaller
fi
./.venv/bin/python - <<'PYCHK'
import importlib.metadata as md
import webview, PyInstaller
print("pywebview", md.version("pywebview"), "/ pyinstaller", md.version("pyinstaller"))
PYCHK

echo "==> [3/6] 准备源码与图标"
cp -f "$SCRIPT_SRC" wifi_monitor.py
ICNS_ARGS=()
if [[ -f assets/icon.png ]]; then
  rm -rf assets/icon.iconset && mkdir -p assets/icon.iconset
  # 标准 macOS iconset 档位：1x 与 2x 全覆盖，从 1024px 源图缩放
  sips -z 16 16      assets/icon.png --out assets/icon.iconset/icon_16x16.png      >/dev/null
  sips -z 32 32      assets/icon.png --out assets/icon.iconset/icon_16x16@2x.png  >/dev/null
  sips -z 32 32      assets/icon.png --out assets/icon.iconset/icon_32x32.png     >/dev/null
  sips -z 64 64      assets/icon.png --out assets/icon.iconset/icon_32x32@2x.png  >/dev/null
  sips -z 128 128    assets/icon.png --out assets/icon.iconset/icon_128x128.png   >/dev/null
  sips -z 256 256    assets/icon.png --out assets/icon.iconset/icon_128x128@2x.png >/dev/null
  sips -z 256 256    assets/icon.png --out assets/icon.iconset/icon_256x256.png   >/dev/null
  sips -z 512 512    assets/icon.png --out assets/icon.iconset/icon_256x256@2x.png >/dev/null
  sips -z 512 512    assets/icon.png --out assets/icon.iconset/icon_512x512.png   >/dev/null
  sips -z 1024 1024  assets/icon.png --out assets/icon.iconset/icon_512x512@2x.png >/dev/null
  iconutil -c icns assets/icon.iconset -o assets/icon.icns
  ICNS_ARGS=(--icon assets/icon.icns)
  echo "图标: assets/icon.icns 生成完毕（含全部 1x/2x 档位）"
else
  echo "警告：assets/icon.png 不存在，使用默认图标"
fi

echo "==> [4/6] PyInstaller 打包（.app，非沙盒，可调 nettop）"
rm -f "${APP_NAME}.spec" wifi_monitor.spec    # 每次由 pyinstaller 重新生成，避免残留误导
./.venv/bin/pyinstaller --noconfirm --clean --windowed \
  --name "$APP_NAME" "${ICNS_ARGS[@]}" \
  --osx-bundle-identifier "cn.luke.wifimatrix" \
  wifi_monitor.py
[[ -d "dist/${APP_NAME}.app" ]] || { echo "错误：未生成 .app，请把上面的完整输出发我排查"; exit 1; }

# 写入版本号（PyInstaller 默认给 0.0.0）
PLIST="dist/${APP_NAME}.app/Contents/Info.plist"
/usr/libexec/PlistBuddy -c "Set :CFBundleShortVersionString ${APP_VERSION}" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleShortVersionString string ${APP_VERSION}" "$PLIST"
/usr/libexec/PlistBuddy -c "Set :CFBundleVersion ${APP_VERSION}" "$PLIST" 2>/dev/null \
  || /usr/libexec/PlistBuddy -c "Add :CFBundleVersion string ${APP_VERSION}" "$PLIST"
echo "版本: $(/usr/libexec/PlistBuddy -c 'Print :CFBundleShortVersionString' "$PLIST")"

echo "==> [5/6] 临时签名（ad-hoc，免费；本机运行更干净）"
codesign --force --deep --sign - "dist/${APP_NAME}.app" 2>/dev/null || echo "警告：ad-hoc 签名失败（不影响本机使用）"

echo "==> [6/6] 制作 DMG"
DMG_NAME="${APP_NAME}-${APP_VERSION}.dmg"
rm -f "${DMG_NAME}"
if command -v create-dmg >/dev/null 2>&1; then
  create-dmg --volname "$APP_NAME" --window-size 660 420 \
    --icon-size 128 --app-drop-link 480 210 \
    "${DMG_NAME}" "dist/${APP_NAME}.app" || true
fi
if [[ ! -f "${DMG_NAME}" ]]; then
  echo "（未安装 create-dmg，用 hdiutil 兜底：brew install create-dmg 可获得拖拽安装界面）"
  STAGE=$(mktemp -d)
  cp -R "dist/${APP_NAME}.app" "$STAGE/"
  ln -s /Applications "$STAGE/Applications"
  hdiutil create -volname "$APP_NAME" -srcfolder "$STAGE" -ov -format UDZO "${DMG_NAME}"
  rm -rf "$STAGE"
fi

echo
echo "=========================================="
echo "完成！产物："
echo "  App : $PROJ_DIR/dist/${APP_NAME}.app   （版本 ${APP_VERSION}，可直接双击运行）"
echo "  DMG : $PROJ_DIR/${DMG_NAME}"
echo "首次从网上下载打开若被拦：右键 → 打开（仅一次）"
echo "=========================================="

# ------------------------------------------------------------
# 【后补正式签名钩子】日后有 Apple Developer ID（$99/年）时，把下面两步
# 换成：
#   codesign --force --deep --options runtime --timestamp \
#     --sign "Developer ID Application: 你的名字 (TEAMID)" dist/WiFiMatrix.app
#   xcrun notarytool submit WiFiMatrix.dmg --apple-id ... --team-id ... --password ... --wait
#   xcrun stapler staple WiFiMatrix.dmg
# 即可获得「双击直接安装、无任何警告」的对外分发版。
# ------------------------------------------------------------
