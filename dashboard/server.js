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
