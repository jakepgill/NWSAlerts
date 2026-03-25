const { app } = require("electron");
const path = require("path");
const fs = require("fs");

const PREFS_FILE = path.join(app.getPath("userData"), "preferences.json");

const DEFAULTS = {
  // Alert types
  notifyEvents: [
    "Tornado Warning",
    "Tornado Watch",
    "Blizzard Warning",
    "Ice Storm Warning",
    "Hurricane Warning",
    "Flash Flood Emergency",
    "Severe Thunderstorm Warning",
    "Flash Flood Warning",
    "Dust Storm Warning",
    "Extreme Wind Warning",
    "Snow Squall Warning",
  ],
  // Severities
  notifySeverities: ["Extreme", "Severe"],
  // Tags
  notifyTags: [
    "emergency",
    "flash-flood-emergency",
    "pds",
    "tornado-possible",
    "catastrophic",
    "destructive",
    "life-threatening",
  ],
};

function load() {
  try {
    if (fs.existsSync(PREFS_FILE)) {
      const data = JSON.parse(fs.readFileSync(PREFS_FILE, "utf8"));
      return { ...DEFAULTS, ...data };
    }
  } catch (e) {}
  return { ...DEFAULTS };
}

function save(prefs) {
  try {
    fs.writeFileSync(PREFS_FILE, JSON.stringify(prefs, null, 2));
  } catch (e) {}
}

module.exports = { load, save, DEFAULTS };
