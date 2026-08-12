const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const fs = require('fs');
const path = require('path');
const cors = require('cors');
const { execSync } = require('child_process');

const app = express();
const server = http.createServer(app);
const io = new Server(server, {
  cors: { origin: '*' }
});

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

const POSITION_FILE_PATHS = [
  path.join(__dirname, '..', 'positions.json')
];

const TRADE_LOG_PATHS = [
  path.join(__dirname, '..', 'trade_log.csv')
];

let cachedPosition = null;
let cachedTrades = [];
let lastMtime = 0;

function readPositionData() {
  for (const filePath of POSITION_FILE_PATHS) {
    try {
      if (fs.existsSync(filePath)) {
        const stats = fs.statSync(filePath);
        if (stats.mtimeMs !== lastMtime || !cachedPosition) {
          const raw = fs.readFileSync(filePath, 'utf-8');
          if (raw && raw.trim()) {
            try {
              cachedPosition = JSON.parse(raw);
              lastMtime = stats.mtimeMs;
            } catch (parseErr) {
              // File is being written concurrently, retain cached state until write finishes
            }
          }
        }
        return cachedPosition;
      }
    } catch (err) {
      // In-flight read error, fallback to cache
    }
  }
  return cachedPosition;
}

function readTradeLogs() {
  const allTrades = [];
  for (const filePath of TRADE_LOG_PATHS) {
    try {
      if (fs.existsSync(filePath)) {
        const raw = fs.readFileSync(filePath, 'utf-8');
        const lines = raw.trim().split('\n');
        if (lines.length <= 1) continue;
        const headers = lines[0].split(',').map(h => h.trim());
        for (let i = 1; i < lines.length; i++) {
          if (!lines[i].trim()) continue;
          const values = lines[i].split(',').map(v => v.trim());
          const entry = {};
          headers.forEach((h, idx) => {
            entry[h] = values[idx] || '';
          });
          allTrades.push(entry);
        }
      }
    } catch (err) {
      console.error(`Error reading trade log ${filePath}:`, err.message);
    }
  }
  return allTrades.reverse();
}

function getPayload() {
  const pos = readPositionData();
  const trades = readTradeLogs();
  return {
    position: pos,
    trades: trades,
    timestamp: new Date().toISOString()
  };
}

// Watch position file for instant sub-second broadcast
POSITION_FILE_PATHS.forEach(p => {
  if (fs.existsSync(p)) {
    try {
      fs.watch(p, { persistent: false }, () => {
        const payload = getPayload();
        io.emit('update', payload);
      });
    } catch (e) {
      // Ignore watch setup errors
    }
  }
});

// REST APIs
app.get('/api/status', (req, res) => {
  res.json(getPayload());
});

// WebSocket real-time broadcast loop (500ms interval for ultra-smooth responsiveness)
io.on('connection', (socket) => {
  socket.emit('update', getPayload());
});

// ─── CLOUD SYNC: Auto-pull from GitHub every 5 minutes ─────────────
const REPO_ROOT = path.join(__dirname, '..');
let lastSyncTime = null;
let syncStatus = 'idle';

function syncFromCloud() {
  try {
    syncStatus = 'syncing';
    try {
      // Fetch the isolated live-data branch from GitHub
      execSync('git fetch origin live-data:live-data --force', {
        cwd: REPO_ROOT,
        timeout: 15000,
        stdio: 'pipe'
      });
      // Extract positions.json to local workspace
      execSync('git show live-data:positions.json > positions.json', {
        cwd: REPO_ROOT,
        timeout: 5000,
        stdio: 'pipe'
      });
      // Extract trade_log.csv if it exists in the branch
      try {
        execSync('git show live-data:trade_log.csv > trade_log.csv', {
          cwd: REPO_ROOT,
          timeout: 5000,
          stdio: 'pipe'
        });
      } catch (err) {
        // Skip if trade_log.csv doesn't exist in live-data branch yet
      }
    } catch (err) {
      // Fallback: If live-data branch is not on remote yet, do a standard main branch pull
      execSync('git pull --rebase 2>/dev/null || git pull', {
        cwd: REPO_ROOT,
        timeout: 15000,
        stdio: 'pipe'
      });
    }
    lastSyncTime = new Date().toISOString();
    syncStatus = 'ok';
    console.log(`[CLOUD SYNC] Sync completed at ${new Date().toLocaleTimeString('en-IN', { timeZone: 'Asia/Kolkata' })} IST`);
  } catch (err) {
    syncStatus = 'error';
    console.error(`[CLOUD SYNC] Sync failed: ${err.message}`);
  }
}

// Auto-sync every 15 seconds (matches workflow's high-frequency loop)
setInterval(syncFromCloud, 15 * 1000);

// Initial sync on dashboard startup
syncFromCloud();

// Manual sync endpoint (click "Sync Now" button on dashboard)
app.post('/api/sync', (req, res) => {
  syncFromCloud();
  res.json({ status: syncStatus, lastSync: lastSyncTime });
});

app.get('/api/sync-status', (req, res) => {
  res.json({ status: syncStatus, lastSync: lastSyncTime });
});

// WebSocket real-time broadcast loop (500ms interval for ultra-smooth responsiveness)
setInterval(() => {
  io.emit('update', getPayload());
}, 500);

const PORT = process.env.PORT || 3000;
server.listen(PORT, () => {
  console.log(`====================================================`);
  console.log(`🔥 DELTA NEUTRAL NIFTY REAL-TIME DASHBOARD ACTIVE 🔥`);
  console.log(`   URL: http://localhost:${PORT}`);
  console.log(`====================================================`);
});
