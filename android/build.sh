#!/usr/bin/env bash
# Builds lanpad.apk without Gradle: javac -> d8 -> aapt2 -> zipalign -> apksigner.
# Needs a JDK plus, from the Android SDK, build-tools and a platform android.jar. They are
# downloaded into ../.sdk on first run (~150 MB) unless ANDROID_BUILD_TOOLS / ANDROID_JAR are set.
set -euo pipefail
cd "$(dirname "$0")"
ROOT=$(cd .. && pwd)
SDK="$ROOT/.sdk"
BT=${ANDROID_BUILD_TOOLS:-$SDK/android-37.0}
JAR=${ANDROID_JAR:-$SDK/android-11/android.jar}

if [[ ! -x $BT/d8 || ! -f $JAR ]]; then
    mkdir -p "$SDK"
    for z in build-tools_r37_linux.zip platform-30_r03.zip; do
        curl -fsSL -o "$SDK/$z" "https://dl.google.com/android/repository/$z"
        unzip -q -o "$SDK/$z" -d "$SDK"
        rm "$SDK/$z"
    done
fi

OUT=build
rm -rf "$OUT"
mkdir -p "$OUT/classes" "$OUT/dex"

javac --release 11 -nowarn -Xlint:-options -classpath "$JAR" -d "$OUT/classes" \
    $(find src -name '*.java')
"$BT/d8" --release --min-api 26 --lib "$JAR" --output "$OUT/dex" $(find "$OUT/classes" -name '*.class')
"$BT/aapt2" link -I "$JAR" --manifest AndroidManifest.xml -o "$OUT/unsigned.apk"
(cd "$OUT/dex" && "$BT/aapt" add -f ../unsigned.apk classes.dex >/dev/null)
"$BT/zipalign" -f -p 4 "$OUT/unsigned.apk" "$OUT/aligned.apk"

# Debug-signed with the standard Android debug key (created if missing).
KS=${ANDROID_KEYSTORE:-$HOME/.android/debug.keystore}
if [[ ! -f $KS ]]; then
    mkdir -p "$(dirname "$KS")"
    keytool -genkeypair -keystore "$KS" -storepass android -keypass android -alias androiddebugkey \
        -keyalg RSA -keysize 2048 -validity 10000 -dname "CN=Android Debug,O=Android,C=US"
fi
"$BT/apksigner" sign --ks "$KS" --ks-pass pass:android --key-pass pass:android --v4-signing-enabled false \
    --out "$ROOT/lanpad.apk" "$OUT/aligned.apk"
rm -rf "$OUT"
echo "built $ROOT/lanpad.apk"
