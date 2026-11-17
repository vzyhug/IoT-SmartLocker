# PlatformIO post-build hook: produce the complete ESP32 flash image for Wokwi.
Import("env")
from os.path import join


def merge_firmware(source, target, env):
    board = env.BoardConfig()
    build_dir = env.subst("$BUILD_DIR")
    merged = join(build_dir, "firmware.merged.bin")
    esptool = join(
        env.PioPlatform().get_package_dir("tool-esptoolpy"), "esptool.py"
    )
    images = [(offset, env.subst(path)) for offset, path in env.get("FLASH_EXTRA_IMAGES", [])]
    images.append((env.subst("$ESP32_APP_OFFSET"), str(target[0])))

    command = [
        '"$PYTHONEXE"',
        f'"{esptool}"',
        "--chip",
        board.get("build.mcu", "esp32"),
        "merge_bin",
        "-o",
        f'"{merged}"',
        "--flash_mode",
        board.get("build.flash_mode", "dio"),
        "--flash_size",
        board.get("upload.flash_size", "4MB"),
    ]
    for offset, path in images:
        command.extend([str(offset) if isinstance(offset, str) else hex(offset), f'"{path}"'])
    env.Execute(env.VerboseAction(" ".join(command), f"Merging full Wokwi image into {merged}"))


env.AddPostAction("$BUILD_DIR/${PROGNAME}.bin", merge_firmware)
