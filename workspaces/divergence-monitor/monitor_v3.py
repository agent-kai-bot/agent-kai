#!/usr/bin/env python3
"""
RSI Divergence Reversal Monitor - Tool-Based Version
Monitors BTC 1m candles for divergence reversal signals
Uses the built-in tool calling mechanism
"""

import json
import time
import os
import subprocess
from datetime import datetime
from typing import Dict, Any, Optional, Tuple

# Change to workspace directory
os.chdir('/home/atc/git/claude-local-ai-agent/workspaces/divergence-monitor')

class RSIDivergenceMonitor:
    def __init__(self):
        # Configuration
        self.symbol = "BTC"
        self.interval = "1m"
        
        # Parameters
        self.rsi_period = 14
        self.rsi_buy_threshold = 45
        self.rsi_sell_threshold = 55
        self.ema_period = 20
        self.stop_loss_pct = 0.003  # 0.30%
        self.take_profit_pct = 0.006  # 0.60%
        self.cooldown_seconds = 60
        
        # State tracking
        self.last_signal_time = 0
        self.prev_rsi: Optional[float] = None
        self.prev_macd: Optional[float] = None
        
        print(f"✓ RSI Divergence Monitor initialized for {self.symbol}")
        print(f"  Interval: {self.interval}")
    
    def call_tool(self, tool_name: str, **kwargs) -> Any:
        """Call a tool using python_exec"""
        # Build the tool call code
        code = f"""
from tools import {tool_name}
result = {tool_name}(**{kwargs})
print(result)
"""
        result = python_exec(code=code)
        return result
    
    def get_rsi(self) -> float:
        """Get RSI indicator value"""
        rsi_data = self.call_tool('calculate_indicator', 
                                   symbol=self.symbol,
                                   indicator='RSI',
                                   period=self.rsi_period,
                                   interval=self.interval,
                                   limit=20)
        
        # Parse the output
        import re
        match = re.search(r'RSI\(14\) = ([\d.]+)', str(rsi_data))
        if match:
            return float(match.group(1))
        return 50.0
    
    def get_ema(self) -> float:
        """Get EMA indicator value"""
        ema_data = self.call_tool('calculate_indicator',
                                   symbol=self.symbol,
                                   indicator='EMA',
                                   period=self.ema_period,
                                   interval=self.interval,
                                   limit=100)
        
        import re
        match = re.search(r'EMA\(\d+\) = \$([\d,]+\.?\d*)', str(ema_data))
        if match:
            return float(match.group(1).replace(',', ''))
        return 0.0
    
    def get_macd(self) -> Tuple[float, float]:
        """Get MACD value and signal line"""
        macd_data = self.call_tool('calculate_indicator',
                                    symbol=self.symbol,
                                    indicator='MACD',
                                    interval=self.interval,
                                    limit=20)
        
        import re
        macd_match = re.search(r'MACD = ([\d.\-]+)', str(macd_data))
        signal_match = re.search(r'Signal = ([\d.\-]+)', str(macd_data))
        
        macd = float(macd_match.group(1)) if macd_match else 0.0
        signal = float(signal_match.group(1)) if signal_match else 0.0
        return macd, signal
    
    def get_close_price(self) -> float:
        """Get latest close price"""
        price_data = self.call_tool('get_latest_price', symbol=self.symbol)
        
        import re
        match = re.search(r'\$([\d,]+\.?\d*)', str(price_data))
        if match:
            return float(match.group(1).replace(',', ''))
        return 0.0
    
    def check_buy_conditions(self, rsi: float, close: float, ema: float, macd: float) -> bool:
        """
        Check buy conditions:
        - RSI_14 < 45
        - close > EMA_20
        - MACD crosses above 0
        """
        rsi_condition = rsi < self.rsi_buy_threshold
        ema_condition = close > ema
        macd_cross = self.prev_macd is not None and self.prev_macd <= 0 and macd > 0
        
        return rsi_condition and ema_condition and macd_cross
    
    def check_sell_conditions(self, rsi: float, macd: float) -> bool:
        """
        Check sell conditions:
        - RSI_14 > 55
        - OR MACD crosses below 0
        """
        rsi_condition = rsi > self.rsi_sell_threshold
        macd_cross = self.prev_macd is not None and self.prev_macd >= 0 and macd < 0
        
        return rsi_condition or macd_cross
    
    def publish_alert(self, signal_type: str, rsi: float, ema: float, macd: float, 
                     macd_signal: float, close: float):
        """Publish structured alert to NATS"""
        from tools import nats_publish
        
        stop_loss_price = close * (1 - self.stop_loss_pct)
        take_profit_price = close * (1 + self.take_profit_pct)
        
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
            'stop_loss_pct': self.stop_loss_pct,
            'take_profit_pct': self.take_profit_pct,
            'stop_loss_price': round(stop_loss_price, 2),
            'take_profit_price': round(take_profit_price, 2)
        }
        
        nats_publish(subject='agent.alerts.divergence', message=json.dumps(alert))
        print(f"\n🚨 [{signal_type}] Alert published for {self.symbol}")
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
        print(f"   • RSI_14 < {self.rsi_buy_threshold}")
        print(f"   • Close > EMA_{self.ema_period}")
        print(f"   • MACD crosses above 0")
        print(f"\n📉 SELL Conditions:")
        print(f"   • RSI_14 > {self.rsi_sell_threshold}")
        print(f"   • OR MACD crosses below 0")
        print(f"\n⚙️  Risk Management:")
        print(f"   • Stop Loss: {self.stop_loss_pct*100}%")
        print(f"   • Take Profit: {self.take_profit_pct*100}%")
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
                      f"RSI: {rsi:.2f} | EMA: ${ema:.2f} | MACD: {macd:.4f} | "
                      f"Close: ${close:.2f}")
                
                # Check for signals
                signal_detected = False
                
                if self.check_buy_conditions(rsi, close, ema, macd):
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
                    cooldown_remaining = max(0, self.cooldown_seconds - (current_time - self.last_signal_time))
                    print(f"   → No signal (cooldown: {cooldown_remaining:.0f}s remaining)")
                
                # Update previous values
                self.prev_rsi = rsi
                self.prev_macd = macd
                
                # Wait for next check
                time.sleep(self.cooldown_seconds)
                
            except KeyboardInterrupt:
                print("\n\n⏹️  Monitor stopped by user")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                import traceback
                traceback.print_exc()
                time.sleep(10)
    
    def run_single_check(self) -> Dict[str, Any]:
        """Run a single check and return results (for testing)"""
        rsi = self.get_rsi()
        ema = self.get_ema()
        macd, macd_signal = self.get_macd()
        close = self.get_close_price()
        
        buy_signal = self.check_buy_conditions(rsi, close, ema, macd)
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
