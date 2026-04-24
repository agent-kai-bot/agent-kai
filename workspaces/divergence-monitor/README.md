# RSI Divergence Reversal Monitor
Automated trading monitor for BTC on 1m timeframe using RSI divergence signals.

## Configuration

### Parameters
- **Symbol**: BTC
- **Timeframe**: 1m
- **RSI Period**: 14
- **RSI Buy Threshold**: < 45
- **RSI Sell Threshold**: > 55
- **EMA Period**: 20
- **Stop Loss**: 0.30%
- **Take Profit**: 0.60%

### Trading Logic

**BUY Signal** (all conditions must be met):
1. RSI_14 < 45 (oversold condition)
2. Close price > EMA_20 (trend confirmation)
3. MACD crosses above 0 (momentum shift)

**SELL Signal** (either condition):
1. RSI_14 > 55 (overbought condition)
2. OR MACD crosses below 0 (momentum reversal)

## Usage

### Start Monitoring
```bash
cd /home/atc/git/claude-local-ai-agent/workspaces/divergence-monitor
python3 monitor_production.py
```

### Run Single Check (Testing)
```bash
python3 -c "from monitor_production import RSIDivergenceMonitor; m = RSIDivergenceMonitor(); print(m.run_single_check())"
```

### Background Service (Optional)
Create a systemd service file at `/etc/systemd/system/divergence-monitor.service`:

```ini
[Unit]
Description=RSI Divergence Reversal Monitor
After=network.target

[Service]
Type=simple
User=atc
WorkingDirectory=/home/atc/git/claude-local-ai-agent/workspaces/divergence-monitor
ExecStart=/usr/bin/python3 monitor_production.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

Then enable and start:
```bash
sudo systemctl enable divergence-monitor
sudo systemctl start divergence-monitor
```

## Alert Format

Alerts are published to NATS subject `agent.alerts.divergence` with this structure:

```json
{
  "timestamp": "2024-01-01T00:00:00.000000",
  "symbol": "BTC",
  "interval": "1m",
  "signal_type": "BUY",
  "price": 43250.50,
  "indicators": {
    "rsi_14": 42.35,
    "ema_20": 43100.25,
    "macd": 0.0234,
    "macd_signal": 0.0156
  },
  "stop_loss_pct": 0.003,
  "take_profit_pct": 0.006,
  "stop_loss_price": 43120.43,
  "take_profit_price": 43510.01
}
```

## Files

- `monitor_production.py` - Main monitoring script
- `config.json` - Configuration parameters
- `monitor.py` - Legacy/prototype version
- `README.md` - This file

## Monitoring

### Check Status
```bash
systemctl status divergence-monitor
```

### View Logs
```bash
journalctl -u divergence-monitor -f
```

### Restart Service
```bash
sudo systemctl restart divergence-monitor
```

## Notes

- Cooldown period: 60 seconds between alerts to prevent spam
- Runs continuously until stopped
- Uses paper trading (simulated) - no real funds at risk
- Alerts published to NATS for integration with other agents

## Integration

Other agents can subscribe to `agent.alerts.divergence` to receive signals for:
- Automated order placement
- Portfolio rebalancing
- Risk management
- Alert notifications
