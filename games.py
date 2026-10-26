"""
状态机多人文字群游戏引擎 · 数字炸弹 / 掷骰子 / 抽签 / 随机点名
零 LLM 幻觉，纯本地逻辑判定与状态机驱动。
"""
import random
import re
import time
from typing import Dict, Optional, Tuple

# 群游戏状态管理: group_id -> game_state
# 例如: {"game": "bomb", "target": 42, "low": 1, "high": 100, "start_time": 123456, "starter": "nickname"}
active_games: Dict[int, dict] = {}

def get_group_game(group_id: int) -> Optional[dict]:
    return active_games.get(group_id)

def handle_quick_game_commands(text: str) -> Optional[str]:
    """处理掷骰子、抽签、投硬币等单次免状态游戏"""
    t = text.strip()
    
    # 1. 掷骰子 / roll
    # 支持: roll, roll 100, 掷骰子, /roll, 摇骰子
    m_roll = re.match(r"^(?:/)?(?:roll|摇骰子|掷骰子|丢骰子)(?:\s+(\d+))?$", t, re.IGNORECASE)
    if m_roll:
        max_val = int(m_roll.group(1)) if m_roll.group(1) else 100
        if max_val < 1:
            max_val = 6
        elif max_val > 1000000:
            max_val = 1000000
        val = random.randint(1, max_val)
        return f"骰子滚出 {val} 点（1~{max_val}）"

    # 2. 抛硬币
    if t in ("抛硬币", "扔硬币", "硬币", "/coin"):
        res = "正面（字）" if random.random() < 0.5 else "反面（花）"
        return f"硬币落地，是【{res}】"

    # 3. 抽签 / 今日运势
    if t in ("抽签", "求签", "今日运势", "/draw"):
        lots = [
            ("大吉", "今天吃大白饭都不会发胖，想抽卡直接出金！"),
            ("中吉", "平平淡淡才是真，摸摸肥鱼头能转运～"),
            ("小吉", "有些小惊喜在路上，适合今天把任务清一清！"),
            ("末吉", "稳健行事，水群小心被大肥鱼怼哦～"),
            ("大凶", "危！今天出门看路，千万不要碰十连抽！"),
            ("万事如意", "大肥鱼专属庇护，今天你是群里最靓的仔！")
        ]
        lot, desc = random.choice(lots)
        return f"抽到【{lot}】：\n「{desc}」"

    return None

def process_number_bomb(group_id: int, text: str, user_id: int, sender_nick: str) -> Tuple[bool, Optional[str]]:
    """
    处理群内数字炸弹逻辑。
    返回: (is_handled, reply_text)
    """
    t = text.strip()
    now = time.time()
    game = active_games.get(group_id)

    # 发起新游戏
    if t in ("数字炸弹", "开炸弹", "开数字炸弹", "/bomb"):
        if game and game.get("game") == "bomb":
            return True, f"游戏还在进行，当前范围 [{game['low']} ~ {game['high']}]，继续猜"
        
        target = random.randint(2, 99)
        active_games[group_id] = {
            "game": "bomb",
            "target": target,
            "low": 1,
            "high": 100,
            "start_time": now,
            "last_active": now,
            "guesses": 0
        }
        return True, "【数字炸弹】装好了\n范围 1~100，谁猜中谁挨炸。谁先来？"

    # 结束游戏
    if t in ("退出数字炸弹", "结束数字炸弹", "结束炸弹", "/stopbomb"):
        if game and game.get("game") == "bomb":
            target = game["target"]
            active_games.pop(group_id, None)
            return True, f"炸弹拆了，答案是【{target}】"
        return False, None

    # 如果有活跃的炸弹，检测用户输入是否为纯数字
    if game and game.get("game") == "bomb":
        # 超过 5 分钟无人发言自动清理
        if now - game.get("last_active", 0) > 300:
            active_games.pop(group_id, None)
            return False, None

        if t.isdigit():
            val = int(t)
            low = game["low"]
            high = game["high"]
            target = game["target"]

            if val <= low or val >= high:
                return True, f"数字得在 {low} ~ {high} 之间"

            game["guesses"] += 1
            game["last_active"] = now

            if val == target:
                guesses = game["guesses"]
                active_games.pop(group_id, None)
                punishments = [
                    "发一张自己的丑照！",
                    "在群里大喊三声「大肥鱼最可爱最美」！",
                    "给群里任意一个人说一句土味情话！",
                    "发个 1 块钱 3 个的群红包！"
                ]
                p = random.choice(punishments)
                return True, (
                    f"BOOM，炸了\n"
                    f"【{sender_nick}】踩中【{target}】\n"
                    f"猜了 {guesses} 轮，惩罚是：\n{p}"
                )
            elif val < target:
                game["low"] = val
                return True, f"安全，范围缩到【{val} ~ {high}】"
            else:
                game["high"] = val
                return True, f"安全，范围缩到【{low} ~ {val}】"

    return False, None
