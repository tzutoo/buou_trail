# -*- coding: utf-8 -*-
import os
import ccxt
import time
import logging
import requests
import json
from logging.handlers import TimedRotatingFileHandler


class MultiAssetTradingBot:
    def __init__(self, config, feishu_webhook=None, monitor_interval=4):
        os.makedirs('log', exist_ok=True)

        self.leverage = float(config["leverage"])
        self.stop_loss_pct = config["stop_loss_pct"]
        self.low_trail_stop_loss_pct = config["low_trail_stop_loss_pct"]
        self.trail_stop_loss_pct = config["trail_stop_loss_pct"]
        self.higher_trail_stop_loss_pct = config["higher_trail_stop_loss_pct"]
        self.low_trail_profit_threshold = config["low_trail_profit_threshold"]
        self.first_trail_profit_threshold = config["first_trail_profit_threshold"]
        self.second_trail_profit_threshold = config["second_trail_profit_threshold"]
        self.feishu_webhook = feishu_webhook
        self.blacklist = set(config.get("blacklist", []))
        self.monitor_interval = monitor_interval  # 从配置文件读取的监控循环时间

        # 配置交易所
        self.exchange = ccxt.bybit({
            'apiKey': config["apiKey"],
            'secret': config["secret"],
            'timeout': 3000,
            'rateLimit': 50,
            'options': {'defaultType': 'swap'},
            # 'proxies': {'http': 'http://127.0.0.1:10100', 'https': 'http://127.0.0.1:10100'},
        })

        self.exchange.enable_demo_trading(True)

        # 配置日志
        log_file = "log/multi_asset_bot.log"
        logger = logging.getLogger(__name__)
        logger.setLevel(logging.INFO)

        # 使用 TimedRotatingFileHandler 以天为单位进行日志分割
        file_handler = TimedRotatingFileHandler(log_file, when='midnight', interval=1, backupCount=7, encoding='utf-8')
        file_handler.suffix = "%Y-%m-%d"  # 设置日志文件名的后缀格式，例如 multi_asset_bot.log.2024-11-05
        formatter = logging.Formatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        logger.addHandler(console_handler)

        self.logger = logger

        # 用于记录每个持仓的最高盈利值和当前档位
        self.highest_profits = {}
        self.current_tiers = {}
        self.detected_positions = {}

    def send_feishu_notification(self, message):
        if self.feishu_webhook:
            try:
                headers = {'Content-Type': 'application/json'}
                payload = {"msg_type": "text", "content": {"text": message}}
                response = requests.post(self.feishu_webhook, json=payload, headers=headers)
                if response.status_code == 200:
                    self.logger.info("飞书通知发送成功")
                else:
                    self.logger.error("飞书通知发送失败，状态码: %s", response.status_code)
            except Exception as e:
                self.logger.error("发送飞书通知时出现异常: %s", str(e))

    def schedule_task(self):
        self.logger.info("启动主循环，开始执行任务调度...")
        try:
            while True:
                self.monitor_positions()
                time.sleep(self.monitor_interval)
        except KeyboardInterrupt:
            self.logger.info("程序收到中断信号，开始退出...")
        except Exception as e:
            error_message = f"程序异常退出: {str(e)}"
            self.logger.error(error_message)
            self.send_feishu_notification(error_message)

    def fetch_positions(self):
        try:
            positions = self.exchange.fetch_positions()
            return positions
        except Exception as e:
            self.logger.error(f"Error fetching positions: {e}")
            return []

    def reduce_market_order(self, symbol, amount, side):
        try:
            print("reduce_market_order")
            # Set positionIdx based on side
            positionIdx = 1 if side == 'sell' else 2  # 1 for closing long, 2 for closing short
            self.exchange.create_order(
                symbol,
                'market',
                side,
                amount,
                None,
                {
                    'reduceOnly': True,
                    'positionIdx': positionIdx
                }
            )
            self.logger.info(f"Closed position for {symbol} with size {amount}, side: {side}")
            return True
        except Exception as e:
            self.logger.error(f"Error reduce_market_order positions: {e}")
            return False

    def close_position(self, symbol, amount, side):
        try:
            # 获取当前持仓数量
            position = next((pos for pos in self.fetch_positions() if pos['symbol'] == symbol), None)
            if position is None or float(position['contracts']) == 0:
                self.logger.info(f"{symbol} 仓位已平，无需继续平仓")
                return True

            self.reduce_market_order(symbol, amount, side)
            self.logger.info(f"Closed position for {symbol} with size {amount}, side: {side}")
            self.send_feishu_notification(f"Closed position for {symbol} with size {amount}, side: {side}")
            # 清除检测过的仓位及相关数据
            self.detected_positions.pop(symbol, None)
            self.highest_profits.pop(symbol, None)  # 清除最高盈利值
            self.current_tiers.pop(symbol, None)  # 清除当前档位
            return True
        except Exception as e:
            self.logger.error(f"Error closing position for {symbol}: {e}")
            return False

    def monitor_positions(self):
        try:
            positions = self.fetch_positions()
            current_symbols = set(
                position['symbol'] for position in positions if float(position.get('contracts', 0)) != 0)

            closed_symbols = set(self.detected_positions.keys()) - current_symbols
            for symbol in closed_symbols:
                self.logger.info(f"手动平仓检测：{symbol} 已平仓，从监控中移除")
                self.send_feishu_notification(f"手动平仓检测：{symbol} 已平仓，从监控中移除")
                self.detected_positions.pop(symbol, None)

            for position in positions:
                try:
                    symbol = position['symbol']
                    position_amt = float(position.get('contracts', 0))
                    unrealized_pnl = float(position.get('unrealizedPnl', 0))
                    position_value = float(position.get('notional', 0))
                    entryPrice = float(position.get('entryPrice', 0))
                    takeProfitPrice = float(position.get('takeProfitPrice', 0))
                    side = position.get('side', '')

                    if position_amt == 0:
                        continue

                    if symbol in self.blacklist:
                        if symbol not in self.detected_positions:
                            self.send_feishu_notification(f"检测到黑名单品种：{symbol}，跳过监控")
                            self.detected_positions[symbol] = position_amt
                        continue

                    # Calculate position's actual stop loss percentage and adjustment ratio using takeProfitPrice, because they are 1:1 to stopLostPrice which is not set
                    position_stop_loss_pct = abs(takeProfitPrice - entryPrice) / entryPrice * 100
                    adjustment_ratio = self.stop_loss_pct / position_stop_loss_pct if position_stop_loss_pct != 0 else 1

                    # Calculate adjusted thresholds for this position
                    adjusted_thresholds = {
                        'low_trail_profit': self.low_trail_profit_threshold * adjustment_ratio,
                        'first_trail_profit': self.first_trail_profit_threshold * adjustment_ratio,
                        'second_trail_profit': self.second_trail_profit_threshold * adjustment_ratio,
                        'low_trail_stop': self.low_trail_stop_loss_pct * adjustment_ratio,
                        'trail_stop': self.trail_stop_loss_pct * adjustment_ratio,
                        'higher_trail_stop': self.higher_trail_stop_loss_pct * adjustment_ratio
                    }

                    profit_pct = (unrealized_pnl / position_value) * 100 if position_value != 0 else 0

                    if symbol not in self.detected_positions:
                        self.detected_positions[symbol] = position_amt
                        self.highest_profits[symbol] = 0
                        self.current_tiers[symbol] = "无"
                        self.logger.info(f"首次检测到仓位：{symbol}, 仓位数量: {position_amt}, 方向: {side}")
                        self.send_feishu_notification(
                            f"首次检测到仓位：{symbol}, 仓位数量: {position_amt}, 方向: {side}")

                    if position_amt > self.detected_positions[symbol]:
                        self.highest_profits[symbol] = 0
                        self.current_tiers[symbol] = "无"
                        self.detected_positions[symbol] = position_amt
                        self.logger.info(f"{symbol} 新仓检测到，重置最高盈利和档位。")
                        continue

                    highest_profit = self.highest_profits.get(symbol, 0)
                    if profit_pct > highest_profit:
                        highest_profit = profit_pct
                        self.highest_profits[symbol] = highest_profit

                    current_tier = "无"
                    if highest_profit >= adjusted_thresholds['second_trail_profit']:
                        current_tier = "第二档移动止盈"
                    elif highest_profit >= adjusted_thresholds['first_trail_profit']:
                        current_tier = "第一档移动止盈"
                    elif highest_profit >= adjusted_thresholds['low_trail_profit']:
                        current_tier = "低档保护止盈"

                    self.current_tiers[symbol] = current_tier

                    self.logger.info(
                        f"监控 {symbol}，仓位: {position_amt}，方向: {side}，"
                        f"浮动盈亏: {profit_pct:.2f}%，最高盈亏: {highest_profit:.2f}%，当前档位: {current_tier}")

                    if current_tier == "低档保护止盈":
                        self.logger.info(f"回撤到{adjusted_thresholds['low_trail_stop']:.2f}% 止盈")
                        if profit_pct <= adjusted_thresholds['low_trail_stop']:
                            self.logger.info(f"{symbol} 触发低档保护止盈，当前盈亏回撤到: {profit_pct:.2f}%，执行平仓")
                            self.close_position(symbol, abs(position_amt), 'sell' if side == 'long' else 'buy')
                            continue

                    elif current_tier == "第一档移动止盈":
                        trail_stop_loss = highest_profit * (1 - adjusted_thresholds['trail_stop'])
                        self.logger.info(f"回撤到 {trail_stop_loss:.2f}% 止盈")
                        if profit_pct <= trail_stop_loss:
                            self.logger.info(
                                f"{symbol} 达到利润回撤阈值，当前档位：第一档移动止盈，"
                                f"最高盈亏: {highest_profit:.2f}%，当前盈亏: {profit_pct:.2f}%，执行平仓")
                            self.close_position(symbol, abs(position_amt), 'sell' if side == 'long' else 'buy')
                            continue

                    elif current_tier == "第二档移动止盈":
                        trail_stop_loss = highest_profit * (1 - adjusted_thresholds['higher_trail_stop'])
                        self.logger.info(f"回撤到 {trail_stop_loss:.2f}% 止盈")
                        if profit_pct <= trail_stop_loss:
                            self.logger.info(
                                f"{symbol} 达到利润回撤阈值，当前档位：第二档移动止盈，"
                                f"最高盈亏: {highest_profit:.2f}%，当前盈亏: {profit_pct:.2f}%，执行平仓")
                            self.close_position(symbol, abs(position_amt), 'sell' if side == 'long' else 'buy')
                            continue

                    if profit_pct <= -self.stop_loss_pct:
                        self.logger.info(f"{symbol} 触发止损，当前盈亏: {profit_pct:.2f}%，执行平仓")
                        self.close_position(symbol, abs(position_amt), 'sell' if side == 'long' else 'buy')

                except (TypeError, ValueError) as e:
                    self.logger.error(f"Error processing position for {symbol}: {e}")
                    continue

        except Exception as e:
            self.logger.error(f"Error in monitor_positions: {e}")
            time.sleep(self.monitor_interval)


if __name__ == '__main__':
    with open('config.json', 'r') as f:
        config_data = json.load(f)

    # 选择交易平台，假设这里选择 Bybit
    platform_config = config_data['bybit']
    feishu_webhook_url = config_data['feishu_webhook']
    monitor_interval = config_data.get("monitor_interval", 4)  # 默认值为4秒

    bot = MultiAssetTradingBot(platform_config, feishu_webhook=feishu_webhook_url, monitor_interval=monitor_interval)
    bot.schedule_task()
