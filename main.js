const { app, BrowserWindow, Tray, Menu, nativeImage, shell, ipcMain, dialog } = require("electron");
const path = require("path");
const trayManager = require("./tray");
const prefs = require("./preferences");
const { autoUpdater } = require("electron-updater");

let mainWindow = null;
let settingsWindow = null;
let tray = null;

const DASHBOARD_URL = "https://nwsalerts.net";
const APP_NAME = "NWSAlerts";

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on("second-instance", () => {
    if (mainWindow) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1400,
    height: 900,
    minWidth: 900,
    minHeight: 600,
    title: APP_NAME,
    icon: path.join(__dirname, "static", "tray64.png"),
    backgroundColor: "#04060d",
    webPreferences: {
      nodeIntegration: false,
      contextIsolation: true,
      webSecurity: true,
    },
    show: false,
  });

  mainWindow.once("ready-to-show", () => mainWindow.show());
  mainWindow.loadURL(DASHBOARD_URL);

  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    shell.openExternal(url);
    return { action: "deny" };
  });

  mainWindow.on("close", (e) => {
    if (!app.isQuitting) { e.preventDefault(); mainWindow.hide(); }
  });

  mainWindow.on("closed", () => { mainWindow = null; });
  return mainWindow;
}

function openSettingsWindow() {
  if (settingsWindow && !settingsWindow.isDestroyed()) {
    settingsWindow.show();
    settingsWindow.focus();
    return;
  }
  settingsWindow = new BrowserWindow({
    width: 480,
    height: 620,
    title: "Notification Settings — NWSAlerts",
    icon: path.join(__dirname, "static", "tray64.png"),
    backgroundColor: "#04060d",
    resizable: false,
    minimizable: false,
    maximizable: false,
    webPreferences: {
      nodeIntegration: true,
      contextIsolation: false,
    },
  });
  settingsWindow.setMenuBarVisibility(false);
  settingsWindow.loadFile(path.join(__dirname, "settings.html"));
  settingsWindow.on("closed", () => { settingsWindow = null; });
}

// IPC
ipcMain.on("get-prefs", (e) => { e.sender.send("load-prefs", prefs.load()); });
ipcMain.on("save-prefs", (e, updated) => { prefs.save(updated); trayManager.reloadPrefs(); });
ipcMain.on("reset-prefs", (e) => {
  prefs.save({ ...prefs.DEFAULTS });
  trayManager.reloadPrefs();
  e.sender.send("prefs-reset", { ...prefs.DEFAULTS });
});

app.whenReady().then(() => {
  createWindow();
  tray = trayManager.create(mainWindow, openSettingsWindow);

  // Auto updater
  autoUpdater.checkForUpdatesAndNotify();

  autoUpdater.on("update-available", () => {
    dialog.showMessageBox({
      type: "info",
      title: "Update Available",
      message: "A new version of NWSAlerts is available. It will be downloaded in the background.",
      buttons: ["OK"],
    });
  });

  autoUpdater.on("update-downloaded", () => {
    dialog.showMessageBox({
      type: "info",
      title: "Update Ready",
      message: "NWSAlerts has been updated. Restart now to apply the update?",
      buttons: ["Restart Now", "Later"],
    }).then(result => {
      if (result.response === 0) autoUpdater.quitAndInstall();
    });
  });

  app.on("activate", () => {
    if (BrowserWindow.getAllWindows().length === 0) createWindow();
  });
});

app.on("window-all-closed", () => {});
app.on("before-quit", () => { app.isQuitting = true; });
