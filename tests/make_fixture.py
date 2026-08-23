"""Create a small fixture translation project at tests/fixture-project/.

Three Chinese chapters, deliberately avoiding catalogue terms so the glossary
seeds empty and the mock-server balance check passes trivially. Blank lines
exercise empty-line handling in the indexed-lines protocol.

Run: python tests/make_fixture.py [--clean]
"""
import re
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).parent
PROJECT = ROOT / "fixture-project"

CH1 = ["第一章 初雪", "", "天还没亮，院子里已经积了薄薄的一层雪。", "少年推开木门，冷风扑面而来。",
       "“今天要去镇上。”他对自己说。", "", "母亲把两个还热的包子塞进他的怀里，什么也没说。",
       "路很远，雪很滑，但他走得很稳。", "远处的山尖上，第一缕阳光正慢慢亮起来。"]

CH2 = ["第二章 镇上", "", "镇子比他想的热闹。", "卖糖的、卖布的、修鞋的，吆喝声连成一片。",
       "他捏了捏怀里的铜钱，走向街角的旧书摊。", "摊主是个瞎了一只眼的老人，正在打瞌睡。",
       "“这本多少钱？”他指着一本封面发黄的书。", "", "老人睁开眼，笑了：“你识字？”", "他点头。"]

CH3 = ["第三章 旧书", "", "那本书讲的是海。", "他从来没见过海，但书里的每个字都让他闻到了咸味。",
       "“看完了？”老人问。", "“看完了。”", "“那它就是你的了。”", "",
       "他把书贴在胸口，像捧着一团火。", "回家的路上，雪又开始下了。"]


def clean():
    if PROJECT.exists():
        shutil.rmtree(PROJECT)
        print("removed", PROJECT)


def write_chapter(name: str, title: str, lines: list[str]):
    fm = (
        "---\n"
        'source_url: "https://example.com/mock-novel/%s"\n'
        "novel_title: 雪与海\n"
        "chapter_title: %s\n"
        "author: 佚名\n"
        "---\n\n" % (name, title)
    )
    (PROJECT / "source" / name).write_text(fm + "\n".join(lines) + "\n", encoding="utf-8")


def main():
    if "--clean" in sys.argv:
        clean()
        return
    clean()
    (PROJECT / "source").mkdir(parents=True)
    write_chapter("Chapter_0001.md", "第一章 初雪", CH1)
    write_chapter("Chapter_0002.md", "第二章 镇上", CH2)
    write_chapter("Chapter_0003.md", "第三章 旧书", CH3)
    total = sum(len([l for l in ch if l.strip()]) for ch in (CH1, CH2, CH3))
    assert not re.search(r"筑基|练气|金丹|灵石|修士|宗门", "\n".join(CH1 + CH2 + CH3))
    print("fixture project at", PROJECT, "| 3 chapters,", total, "non-empty lines")


if __name__ == "__main__":
    main()
