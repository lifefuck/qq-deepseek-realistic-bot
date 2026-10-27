#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""印象原子层与跨群话题去重的单测。不起网络。

用法：.venv/bin/python -m unittest tests.test_memory -v
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import memory_atoms as MA


class TestDedup(unittest.TestCase):
    def test_exact_duplicate_merges(self):
        notes = []
        for _ in range(5):
            notes = MA.add_note(notes, "阴阳怪气")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["hits"], 4)

    def test_refinement_keeps_longer(self):
        notes = MA.add_note([], "爱跟小鲸鱼对线")
        notes = MA.add_note(notes, "爱跟小鲸鱼对线吵架")
        self.assertEqual(len(notes), 1)
        self.assertEqual(notes[0]["text"], "爱跟小鲸鱼对线吵架")

    def test_same_meaning_different_words_merges(self):
        # 改写句靠二元组重合度兜住
        notes = MA.add_note([], "喜欢投喂可乐")
        notes = MA.add_note(notes, "喜欢投喂可乐喝")
        self.assertEqual(len(notes), 1)

    def test_unrelated_stays_separate(self):
        notes = MA.add_note([], "爱演烂戏")
        notes = MA.add_note(notes, "阴阳怪气")
        notes = MA.add_note(notes, "爱找茬")
        self.assertEqual(len(notes), 3)

    def test_conflict_replaces(self):
        notes = MA.add_note([], "喜欢喝可乐")
        notes = MA.add_note(notes, "不喜欢喝可乐")
        self.assertEqual(len(notes), 1)
        self.assertIn("不", notes[0]["text"])

    def test_old_pipe_string_split(self):
        notes = MA.migrate(["喜欢戳尾巴；爱叫大肥鱼；投喂可乐"])
        self.assertEqual(len(notes), 3)
        self.assertTrue(all(isinstance(n, dict) for n in notes))


class TestLevel(unittest.TestCase):
    def test_rule_levels(self):
        self.assertEqual(MA.classify_level("不许提他前女友"), 0)
        self.assertEqual(MA.classify_level("我最喜欢喝冰可乐"), 4)
        self.assertEqual(MA.classify_level("晚安"), 1)

    def test_interaction_not_personality(self):
        # 「投喂可乐」这类互动流水不该按平常待遇长期驻留
        self.assertEqual(MA.classify_level("喜欢投喂可乐"), 2)
        notes = MA.migrate(["喜欢投喂可乐"])
        gone = MA.apply_decay(notes, now=time.time() + 30 * 86400)
        self.assertEqual(gone, [])

    def test_decay_evicts_transient_only(self):
        now = time.time()
        notes = MA.migrate(["晚安", "不许提他前女友"])
        notes = MA.apply_decay(notes, now=now + 30 * 86400)
        texts = [n["text"] for n in notes]
        self.assertNotIn("晚安", texts)          # 1 级一个月早该散了
        self.assertIn("不许提他前女友", texts)    # 0 级永不衰减

    def test_decay_is_idempotent(self):
        now = time.time()
        notes = MA.migrate(["今天好热"])
        a = MA.apply_decay([dict(n) for n in notes], now=now + 86400)[0]["d"]
        b = MA.apply_decay([dict(n) for n in notes], now=now + 86400)[0]["d"]
        self.assertAlmostEqual(a, b, places=6)


class TestRender(unittest.TestCase):
    def test_budget_respected(self):
        notes = MA.migrate(["一" * 60, "二" * 60, "三" * 60])
        text, used = MA.render(notes, budget=110)
        self.assertLessEqual(len(text), 130)
        self.assertEqual(len(used), 1)   # 第二条放不下就不放，不硬截断

    def test_important_first(self):
        notes = MA.migrate(["晚安", "我最讨厌别人叫我胖鱼", "哈哈"])
        text, _ = MA.render(notes, budget=40)
        self.assertIn("讨厌", text)

    def test_same_theme_only_once(self):
        # 「投喂可乐 / 友好投喂 / 投喂但调侃」是同一种互动，注入时只留一条
        notes = MA.migrate(["投喂可乐", "友好投喂", "投喂但调侃", "想吃鱼"])
        text, _ = MA.render(notes)
        self.assertEqual(text.count("投喂"), 1)

    def test_empty(self):
        self.assertEqual(MA.render([]), ("", []))


class TestTopicPool(unittest.TestCase):
    """可乐问题：同一个梗刷屏时，池子里只能留一条。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        os.unlink(self.tmp.name)
        import cross_memory
        self.mgr = cross_memory.CrossGroupTopicManager(file_path=self.tmp.name)

    def tearDown(self):
        if os.path.exists(self.tmp.name):
            os.unlink(self.tmp.name)

    def test_cola_spam_collapses(self):
        lines = [
            "可乐，棒棒糖，小蛋糕全是你的",
            "请你喝一箱可乐",
            "我只偏心你啊，再给你一箱冰镇可乐",
            "接着给你一箱可乐喝吧",
        ]
        for line in lines:
            self.mgr.record_topic(111, "群友", line)
        prompt = self.mgr.get_recent_topics_prompt(exclude_group_id=999, limit=3)
        self.assertLessEqual(prompt.count("·"), 1)

    def test_other_group_only(self):
        self.mgr.record_topic(111, "群友", "今天群里聊了原神新版本")
        self.assertEqual(self.mgr.get_recent_topics_prompt(exclude_group_id=111), "")

    def test_no_stale_topics(self):
        self.mgr.record_topic(111, "群友", "昨天在聊一个新番剧")
        for item in self.mgr.topics:
            item["ts"] = time.time() - 24 * 3600
        self.assertEqual(self.mgr.get_recent_topics_prompt(exclude_group_id=999), "")

    def test_cooldown(self):
        self.mgr.record_topic(111, "群友", "群里在聊原神新版本前瞻")
        self.assertTrue(self.mgr.get_recent_topics_prompt(exclude_group_id=999))
        self.assertEqual(self.mgr.get_recent_topics_prompt(exclude_group_id=999), "")

    def test_cq_code_stripped(self):
        self.mgr.record_topic(111, "群友", "[CQ:at,qq=168188902] 小说太狠了真看哭了")
        content = self.mgr.topics[-1]["content"]
        self.assertNotIn("CQ:", content)

    def test_played_out_topic_dropped(self):
        # 同一个梗聊到第八轮就不该再当新鲜背景喂给模型
        for _ in range(9):
            self.mgr.record_topic(111, "群友", "接着给你一箱冰镇可乐吧")
        self.assertEqual(self.mgr.get_recent_topics_prompt(exclude_group_id=999), "")


if __name__ == "__main__":
    unittest.main()
