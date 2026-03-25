const { Tray, Menu, nativeImage, Notification, app } = require("electron");
const path = require("path");
const https = require("https");
const prefs = require("./preferences");

const DATA_URL = "https://nwsalerts.net/data_shown";
const POLL_INTERVAL_MS = 15000;

let trayInstance = null;
let mainWindowRef = null;
let openSettingsRef = null;
let lastAlertIds = new Set();
let isFirstPoll = true;
let currentPrefs = prefs.load();

function reloadPrefs() {
  currentPrefs = prefs.load();
}

function shouldNotify(alert) {
  const event = (alert.event || "").trim();
  const severity = (alert.severity || "").trim();
  const tags = (alert.tags || []).map(t => typeof t === "object" ? t.key : t);

  // Check event match
  if ((currentPrefs.notifyEvents || []).includes(event)) return true;

  // Check severity match
  if ((currentPrefs.notifySeverities || []).includes(severity)) return true;

  // Check tag match
  if (tags.some(t => (currentPrefs.notifyTags || []).includes(t))) return true;

  return false;
}

function sendNotification(alert) {
  if (!Notification.isSupported()) return;
  const tags = (alert.tags || []).map(t => typeof t === "object" ? t.key : t);
  const tagStr = tags.length ? ` — ${tags.join(", ").toUpperCase()}` : "";
  new Notification({
    title: `⚠ ${alert.event || "Weather Alert"}${tagStr}`,
    body: (alert.area || "").trim() || "Active alert — check NWSAlerts for details.",
    icon: path.join(__dirname, "static", "logo.png"),
    urgency: "critical",
  }).show();
}

function fetchData(callback) {
  https.get(DATA_URL, (res) => {
    let data = "";
    res.on("data", (chunk) => { data += chunk; });
    res.on("end", () => {
      try { callback(null, JSON.parse(data)); }
      catch (e) { callback(e); }
    });
  }).on("error", callback);
}

function updateTray(count) {
  if (!trayInstance) return;
  const label = count > 0
    ? `NWSAlerts — ${count} Active Alert${count !== 1 ? "s" : ""}`
    : "NWSAlerts — No Active Alerts";
  trayInstance.setToolTip(label);

  const menu = Menu.buildFromTemplate([
    {
      label: count > 0 ? `${count} Active Alert${count !== 1 ? "s" : ""}` : "No Active Alerts",
      enabled: false,
    },
    { type: "separator" },
    {
      label: "Open NWSAlerts",
      click: () => {
        if (mainWindowRef) { mainWindowRef.show(); mainWindowRef.focus(); }
      },
    },
    {
      label: "Notification Settings",
      click: () => { if (openSettingsRef) openSettingsRef(); },
    },
    {
      label: "Test Notification",
      click: () => {
        sendNotification({
          event: "Tornado Warning",
          area: "Test County, Test State",
          tags: ["emergency"],
        });
      },
    },
    { type: "separator" },
    {
      label: "Quit",
      click: () => { app.isQuitting = true; app.quit(); },
    },
  ]);
  trayInstance.setContextMenu(menu);
}

function poll() {
  fetchData((err, data) => {
    if (err) return;
    const alerts = data.alerts || [];
    const count = alerts.length;

    const currentIds = new Set(alerts.map(a => a.id));
    if (!isFirstPoll) {
      for (const alert of alerts) {
        if (!lastAlertIds.has(alert.id) && shouldNotify(alert)) {
          sendNotification(alert);
        }
      }
    }
    isFirstPoll = false;
    lastAlertIds = currentIds;
    updateTray(count);
  });
}

function create(mainWindow, openSettings) {
  mainWindowRef = mainWindow;
  openSettingsRef = openSettings;

  const iconPath = path.join(__dirname, "static", "tray16.png");
  const icon = nativeImage.createFromPath(iconPath);
  trayInstance = new Tray(icon);
  trayInstance.setToolTip("NWSAlerts");

  trayInstance.on("click", () => {
    if (mainWindow.isVisible()) {
      mainWindow.hide();
    } else {
      mainWindow.show();
      mainWindow.focus();
    }
  });

  updateTray(0);
  poll();
  setInterval(poll, POLL_INTERVAL_MS);

  return trayInstance;
}

module.exports = { create, reloadPrefs };
