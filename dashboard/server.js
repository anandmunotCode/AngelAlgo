const express = require('express');
const http = require('http');
const { Server } = require('socket.io');
const fs = require('fs');
const path = require('path');
const cors = require('cors');

const app = express();
const server = http.createServer(app);
const io = new Server(server, {
  cors: { origin: '*' }
});

app.use(cors());
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

const POSITION_FILE_PATHS = [
  path.join(__dirname, '..', 'positions.json'),
  path.join(__dirname, '..', 'position.json'),
  path.join(__dirname, '..', 'Delta_Neutral_Nifty', 'data', 'position.json'),
  path.join(__dirname, '..', 'Delta_Neutral_Nifty', 'data', 'positions.json')
];

const TRADE_LOG_PATHS = [
  path.join(__dirname, '..', 'trade_log.csv'),
  path.join(__dirname, '..', 'paper_trades_log.csv'),
  path.join(__dirname, '..', 'Delta_Neutral_Nifty', 'data', 'trade_log.csv')
];

function readPositionData() {
  for (const filePath of POSITION_FILE_PATHS) {
    try {
      if (fs.existsSync(filePath)) {
        const raw = fs.readFileSync(filePath, 'utf-8');
        if (raw && raw.trim()) {
          return JSON.parse(raw);
        }
      }
    } catch (err) {
      console.error(`Error reading position file ${filePath}:`, err.message);
    }
  }
  return null;
}

function readTradeLogs() {
  for (const filePath of TRADE_LOG_PATHS) {
    try {
      if (fs.existsSync(filePath)) {
        const raw = fs.readFileSync(filePath, 'utf-8');
        const lines = raw.trim().split('\n');
        if (lines.length <= 1) continue;
        const headers = lines[0].split(',').map(h => h.trim());
        const trades = [];
        for (let i = 1; i < lines.length; i++) {
          if (!lines[i].trim()) continue;
          const values = lines[i].split(',').map(v => v.trim());
          const entry = {};
          headers.forEach((h, idx) => {
            entry[h] = values[idx] || '';
          });
          trades.push(entry);
        }
        if (trades.length > 0) return trades.reverse();
      }
    } catch (err) {
      console.error(`Error reading trade log ${filePath}:`, err.message);
    }
  }
  return [];
}

// REST APIs
app.get('/api/status', (req, res) => {
  const pos = readPositionData();
  const trades = readTradeLogs();
  res.json({ position: pos, trades: trades, timestamp: new Date().toISOString() });
});

// WebSocket real-time broadcast loop (1000ms interval)
io.on('connection', (socket) => {
  console.log('Client connected to dashboard WebSocket');
  
  // Initial send
  const pos = readPositionData();
  const trades = readTradeLogs();
  socket.emit('update', { position: pos, trades: trades, timestamp: new Date().toISOString() });

  socket.on('disconnect', () => {
    console.log('Client disconnected');
  });
});

setInterval(() => {
  const pos = readPositionData();
  const trades = readTradeLogs();
  io.emit('update', { position: pos, trades: trades, timestamp: new Date().toISOString() });
}, 1000);

const PORT = process.env.PORT || 3000;
server.listen(PORT, () => {
  console.log(`====================================================`);
  console.log(`🔥 DELTA NEUTRAL NIFTY DASHBOARD RUNNING 🔥`);
  console.log(`   URL: http://localhost:${PORT}`);
  console.log(`====================================================`);
});
