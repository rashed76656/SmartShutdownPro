# 💤 Smart Shutdown Pro (SST Pro)

![Windows 11 Supported](https://shields.io)
![Python Version](https://shields.io)
![License](https://shields.io)

**Smart Shutdown Pro** is a premium, modern, and minimalist Windows power-management utility designed to replace tedious command-line shutdown triggers. Built with Python and `CustomTkinter`, it offers a sleek Windows 11-style curved GUI, multi-threaded execution, and intelligent resource automation to seamlessly manage your transition to sleep.

---

## ✨ Core Features

- **🌐 English GUI Matrix:** A clean, high-contrast typography interface with a real-time digital countdown array (`HH:MM:SS`).
- **⚡ Advanced Power Actions:** Supports instant execution mapping for **Shutdown**, **Sleep**, **Hibernate**, and **Lock Screen**.
- **⏱️ Dual Execution Matrix:** 
  - **Minute Timer:** Standard countdown entry with rapid-action presets (30 Min, 45 Min, 60 Min).
  - **Clock Scheduler:** Schedule accurate clock-based targets (`HH:MM`) in a 24-hour cycle with auto-tomorrow validation.
- **🔋 Smart Intelligent Automation (Advanced):**
  - *Battery Protection:* Automatically forces safe system hibernation when the laptop drops under 20% charge on DC power.
  - *CPU Resource Monitor:* Triggers actions automatically if global hardware utilization drops under 5% for 3 minutes straight.
  - *Network/Download Detection:* Auto-shutdown sequence activates if active downloading rates fall under 5 KB/s for 2 minutes.
- **🔄 System Tray Integration:** Clicking the close (`X`) button hides the window directly into the taskbar system tray. The countdown safely survives in the background.
- **🚨 60-Second Critical Fail-Safe:** At the final 60 seconds, the app forces itself to the top of all active screens, pulsing an acoustic warning beep with a giant **ABORT 🛑** button to quickly prevent accidental closures.

---

## 🎨 Preview Theme & Interface
The app loads a luxurious **"Deep Charcoal Neon"** profile (`assets/cyber_glow.json`) engineered for comfortable dark room/night use. All frames and input nodes feature modern 8px to 12px rounded aesthetic constraints.

---

## 📁 Repository Structure
```text
Smart-Shutdown-Pro/
│
├── assets/
│   ├── app_icon.ico       # Main executable icon
│   ├── app_icon.png       # UI branding visual assets (512x512)
│   └── cyber_glow.json    # Premium CustomTkinter theme profile
│
├── src/
│   └── main.py            # Main application source code
│
└── requirements.txt       # Global dependency manifests
```

---

## 🛠️ Installation & Setup

### Prerequisites
Make sure you have Python 3.10+ installed on your Windows machine.

1. **Clone the repository:**
   ```bash
   git clone https://github.com
   cd Smart-Shutdown-Pro
   ```

2. **Install core dependencies:**
   ```bash
   pip install -r requirements.txt
   ```
   *(Or manually install: `pip install customtkinter plyer psutil pillow pystray`)*

3. **Run the source profile:**
   ```bash
   python src/main.py
   ```

---

## 📦 Compiling to Standalone Executable (.exe)

To package everything into a single, portable binary with no background terminal attachment, execute the following PyInstaller compiler command inside your root runspace:

```bash
pyinstaller --noconfirm --onefile --windowed --icon="assets/app_icon.ico" --add-data "assets;assets" src/main.py
```

After compilation is successfully finished, navigate inside the newly populated `dist/` directory to fetch your clean **`smart_shutdown_pro.exe`** utility file.

---

## 📄 License
This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

## 🤝 Contributing
Contributions, issues, and feature requests are welcome! Feel free to check the issues page if you want to submit updates.
