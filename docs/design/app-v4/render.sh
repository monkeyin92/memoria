cd /workspace/memoria-ui/app-v4
n=(x login consent home summary attention usage content roles memory profile)
for i in $(seq 1 10); do google-chrome --headless=new --no-sandbox --hide-scrollbars --force-device-scale-factor=2 --window-size=390,844 --screenshot=$(printf %02d $i)-${n[$i]}.png "file://$PWD/app.html?p=$i" 2>/dev/null; done
google-chrome --headless=new --no-sandbox --hide-scrollbars --force-device-scale-factor=2 --window-size=2250,2120 --screenshot=all-pages.png "file://$PWD/app.html" 2>/dev/null
