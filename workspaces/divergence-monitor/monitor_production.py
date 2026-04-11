#!/usr/bin/env python3
"""
RSI Divergence Reversal Monitor - Production Version
Monitors BTC 1m candles for divergence reversal signals
"""

import json
import time
import sys
from datetime import datetime
from typing import Dict, Any, Optional, List, Tuple

# Add workspace to path
sys.path.insert(0, '/home/atc/git/claude-local-ai-agent')

class RSIDivergenceMonitor:
    def __init__(self, config_path: str = "config.json"):
        # Load config
        with open(config_path, 'r') as f:
            self.config = json.load(f)
        
        self.symbol = self.config['symbol']
        self.interval = self.config['interval']
        self.params = self.config['parameters']
        self.alert_subject = self.config['alert_subject']
        self.cooldown_seconds = self.config['cooldown_seconds']
        self.check_interval = self.config['check_interval_seconds']
        
        # State tracking
        self.last_signal_time = 0
        self.prev_rsi: Optional[float] = None
        self.prev_macd: Optional[float] = None
        self.prev_macd_signal: Optional[float] = None
        
        print(f"✓ RSI Divergence Monitor initialized for {self.symbol}")
        print(f"  Interval: {self.interval}")
        print(f"  RSI Period: {self.params['rsi_period']}")
        print(f"  EMA Period: {self.params['ema_period']}")
    
    def get_latest_price(self) -> float:
        """Get current price using get_latest_price tool"""
        from tools import get_latest_price
        return get_latest_price(symbol=self.symbol)
    
    def get_rsi(self) -> float:
        """Get RSI indicator value"""
        from tools import calculate_indicator
        rsi_data = calculate_indicator(
            symbol=self.symbol,
            indicator='RSI',
            period=self.params['rsi_period'],
            interval=self.interval,
            limit=20
        )
        # Get latest RSI value
        if rsi_data and 'values' in rsi_data:
            return rsi_data['values'][-1]
        return 50.0
    
    def get_ema(self) -> float:
        """Get EMA indicator value"""
        from tools import calculate_indicator
        ema_data = calculate_indicator(
            symbol=self.symbol,
            indicator='EMA',
            period=self.params['ema_period'],
            interval=self.interval,
            limit=20
        )
        if ema_data and 'values' in ema_data:
            return ema_data['values'][-1]
        return 0.0
    
    def get_macd(self) -> Tuple[float, float]:
        """Get MACD value and signal line"""
        from tools import calculate_indicator
        macd_data = calculate_indicator(
            symbol=self.symbol,
            indicator='MACD',
            interval=self.interval,
            limit=20
        )
        # MACD returns dict with macd and signal values
        if macd_data and 'values' in macd_data:
            values = macd_data['values']
            # Last value is the MACD line, second to last is signal
            macd_line = values[-1] if isinstance(values[-1], (int, float)) else values[-1].get('macd', 0)
            signal_line = values[-1] if isinstance(values[-1], (int, float)) else values[-1].get('signal', 0)
            return macd_line, signal_line
        return 0.0, 0.0
    
    def get_close_price(self) -> float:
        """Get latest close price from OHLCV"""
        from tools import query_ohlcv
        ohlcv_data = query_ohlcv(
            symbol=self.symbol,
            interval=self.interval,
            limit=1
        )
        if ohlcv_data and 'candles' in ohlcv_data and len(ohlcv_data['candles']) > 0:
            return ohlcv_data['candles'][-1]['close']
        return self.get_latest_price()
    
    def check_buy_conditions(self, rsi: float, close: float, ema: float, macd: float, macd_signal: float) -> bool:
        """
        Check buy conditions:
        - RSI_14 < 45
        - close > EMA_20
        - MACD crosses above 0
        """
        rsi_condition = rsi < self.params['rsi_buy_threshold']
        ema_condition = close > ema
        macd_cross = self.prev_macd is not None and self.prev_macd <= 0 and macd > 0
        
        return rsi_condition and ema_condition and macd_cross
    
    def check_sell_conditions(self, rsi: float, macd: float) -> bool:
        """
        Check sell conditions:
        - RSI_14 > 55
        - OR MACD crosses below 0
        """
        rsi_condition = rsi > self.params['rsi_sell_threshold']
        macd_cross = self.prev_macd is not None and self.prev_macd >= 0 and macd < 0
        
        return rsi_condition or macd_cross
    
    def publish_alert(self, signal_type: str, rsi: float, ema: float, macd: float, 
                     macd_signal: float, close: float):
        """Publish structured alert to NATS"""
        from tools import nats_publish
        
        stop_loss_price = close * (1 - self.params['stop_loss_pct'])
        take_profit_price = close * (1 + self.params['take_profit_pct'])
        
        alert = {
            'timestamp': datetime.utcnow().isoformat(),
            'symbol': self.symbol,
            'interval': self.interval,
            'signal_type': signal_type,
            'price': close,
            'indicators': {
                'rsi_14': round(rsi, 2),
                'ema_20': round(ema, 2),
                'macd': round(macd, 4),
                'macd_signal': round(macd_signal, 4)
            },
            'stop_loss_pct': self.params['stop_loss_pct'],
            'take_profit_pct': self.params['take_profit_pct'],
            'stop_loss_price': round(stop_loss_price, 2),
            'take_profit_price': round(take_profit_price, 2)
        }
        
        nats_publish(subject=self.alert_subject, message=json.dumps(alert))
        print(f"🚨 [{signal_type}] Alert published for {self.symbol}")
        print(f"   Price: ${close:.2f} | RSI: {rsi:.2f} | MACD: {macd:.4f}")
        print(f"   Stop Loss: ${stop_loss_price:.2f} | Take Profit: ${take_profit_price:.2f}")
    
    def run_monitoring_loop(self):
        """Main monitoring loop"""
        print("\n" + "="*60)
        print(f"📊 RSI Divergence Reversal Monitor")
        print(f"   Symbol: {self.symbol}")
        print(f"   Interval: {self.interval}")
        print("="*60)
        print(f"📈 BUY Conditions:")
        print(f"   • RSI_14 < {self.params['rsi_buy_threshold']}")
        print(f"   • Close > EMA_{self.params['ema_period']}")
        print(f"   • MACD crosses above 0")
        print(f"\n📉 SELL Conditions:")
        print(f"   • RSI_14 > {self.params['rsi_sell_threshold']}")
        print(f"   • OR MACD crosses below 0")
        print(f"\n⚙️  Risk Management:")
        print(f"   • Stop Loss: {self.params['stop_loss_pct']*100}%")
        print(f"   • Take Profit: {self.params['take_profit_pct']*100}%")
        print(f"   • Cooldown: {self.cooldown_seconds}s")
        print("="*60 + "\n")
        
        iteration = 0
        while True:
            try:
                iteration += 1
                current_time = time.time()
                
                # Fetch all indicators
                rsi = self.get_rsi()
                ema = self.get_ema()
                macd, macd_signal = self.get_macd()
                close = self.get_close_price()
                
                # Log current state
                print(f"[{datetime.now().strftime('%H:%M:%S')}] "
                      f"RSI: {rsi:.2f} | EMA: {ema:.2f} | MACD: {macd:.4f} | "
                      f"Close: ${close:.2f}")
                
                # Check for signals
                signal_detected = False
                
                if self.check_buy_conditions(rsi, close, ema, macd, macd_signal):
                    if current_time - self.last_signal_time > self.cooldown_seconds:
                        self.publish_alert('BUY', rsi, ema, macd, macd_signal, close)
                        self.last_signal_time = current_time
                        signal_detected = True
                
                elif self.check_sell_conditions(rsi, macd):
                    if current_time - self.last_signal_time > self.cooldown_seconds:
                        self.publish_alert('SELL', rsi, ema, macd, macd_signal, close)
                        self.last_signal_time = current_time
                        signal_detected = True
                
                if not signal_detected:
                    print(f"   → No signal (cooldown: {self.cooldown_seconds - (current_time - self.last_signal_time):.0f}s remaining)")
                
                # Update previous values
                self.prev_rsi = rsi
                self.prev_macd = macd
                self.prev_macd_signal = macd_signal
                
                # Wait for next check
                time.sleep(self.check_interval)
                
            except KeyboardInterrupt:
                print("\n\n⏹️  Monitor stopped by user")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                time.sleep(10)
    
    def run_single_check(self) -> Dict[str, Any]:
        """Run a single check and return results (for testing)"""
        rsi = self.get_rsi()
        ema = self.get_ema()
        macd, macd_signal = self.get_macd()
        close = self.get_close_price()
        
        buy_signal = self.check_buy_conditions(rsi, close, ema, macd, macd_signal)
        sell_signal = self.check_sell_conditions(rsi, macd)
        
        return {
            'timestamp': datetime.utcnow().isoformat(),
            'rsi_14': rsi,
            'ema_20': ema,
            'macd': macd,
            'macd_signal': macd_signal,
            'close': close,
            'buy_signal': buy_signal,
            'sell_signal': sell_signal
        }


def main():
    monitor = RSIDivergenceMonitor()
    monitor.run_monitoring_loop()


if __name__ == "__main__":
    main()
