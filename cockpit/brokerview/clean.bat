
echo Cleaning broker viewer...
rmdir /s /q node_modules 2>nul
rmdir /s /q "=" 2>nul
rmdir /s /q .cache 2>nul
rmdir /s /q .vite 2>nul

# for rebuild only
#rmdir /s /q dist 2>nul
#rmdir /s /q build 2>nul
