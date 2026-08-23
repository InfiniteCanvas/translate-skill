# Expand glossary seed catalogues: xianxia, wuxia, gaming/meme terms

## Context (verified)

- Seed catalogues live in `novel-translator/assets/catalogues/`. Today there is one file, `zh-cultivation.json`, with 43 terms. `init`/`seed` auto-discover **every** `*.json` there with a matching `"language"` (`translate.py:200`), so new catalogue files need **zero code changes**.
- `glossary.seed()` copies the whole catalogue term dict into the project glossary — so **`alt_translations` in catalogue entries survive seeding** and feed the balance gate (confirmed in `glossary.py:162-165`). This is key: the gate (balance.py) requires the canonical rendering (or an alt) to appear ≥ ceil(25% of source occurrences), and zero matches with ≥2 source occurrences hard-fails a chapter. Terms with variable renderings therefore need generous `alt_translations`; multi-word translations match as case-insensitive whitespace-flexible phrases with final-word inflection, single words ≥5 letters get Levenshtein-2 fuzzy matching.
- Seeding only activates terms appearing ≥ `seed_min_count` (3) times in the corpus, so a large catalogue is safe — unused terms never enter a project glossary.
- The installed skill at `C:\Users\chana\.zcode\skills\novel-translator` is a deployed copy of this repo's skill and must be synced after the change (step 6).
- Automated tests use `tests/fixture-project`, whose glossary must stay empty (fixture text deliberately avoids catalogue terms) — new terms must be checked against fixture text (step 4).

## Conventions (following existing file + your choices)

- Wuxiaworld-classic realm ladder (already present), extended upward through immortal ranks.
- 魔/妖 → "demonic" family, matching existing entries (魔修 "demonic cultivator", 妖兽 "demonic beast").
- Address terms **translated**: 师兄 "Senior Brother", 师妹 "Junior Sister", 师叔 "Martial Uncle", 师尊 "Master". 仙子 → **"Fairy"**. 道友 → "Fellow Daoist".
- Genre loanwords stay in pinyin where that is the convention: Jianghu, Wulin, dantian, qi (in compounds), Long Aotian.
- Memes use **fan-literal primary** ("salted fish", "lying flat", "eating melon", "face-slapping") with naturalized alts.
- Every entry gets: `source`, traditional-Chinese `variants` where the form differs, `translation`, `alt_translations` where more than one rendering is accepted, `category` (from the existing enum: place/person/org/skill/technique/level/state/item/honorific/other), one-sentence `definition`.

## Curation safety rules (protect the balance gate)

1. **No single-CJK-character sources** (气/道/剑/刀/丹/肝 would count every compound occurrence and inflate the coverage floor into false hard-fails). Sole exception: 叮 → "Ding!" (one-to-one in system novels).
2. Flexible common nouns (高手, 天才, 任务, 技能) included **only** with `alt_translations` covering the standard renderings.
3. Alphanumeric slang (666, yyds, 23333) and purely contextual slang (真香, 破防, emo了, 沙雕) are **excluded on purpose** — the gate's letter-token matching can't reliably count them, so they'd cause false failures. The GLOSSARY_EXPAND stage will catch them per-novel anyway.
4. No duplicate sources or variants across the three catalogues (first-seed wins silently; verified in step 4).
5. Contested 先天/后天 realms are left out entirely (per-novel overrides).

## File changes

### 1. Expand `novel-translator/assets/catalogues/zh-cultivation.json` (43 → ~130 terms)
Additions grouped: cultivation verbs/nouns (修炼 修仙 修真 修行 修真界 境界 突破 瓶颈 圆满 结丹 飞升); stage furniture (初期/中期/后期/巅峰, 半步); immortal ranks (真仙 金仙 大罗金仙 天仙 地仙 仙帝 大帝 圣人); worlds (仙界 神界 凡间 凡人 三界 上界 下界 洞天 福地 灵脉 仙门 圣地 王朝); dao/cosmos (道心 大道 天道 逆天 悟道 顿悟 参悟 领悟 阴阳 五行 因果 功德 轮回 法则 本源 天地 天命); address honorifics (道友 前辈 晚辈 仙子 道长 真人 上仙 老祖 大能 天骄 老怪物 + 师父/师尊/师兄/师弟/师姐/师妹/师叔/师伯/大师兄 cluster); persons/orgs (魔道 魔头 妖族 灵兽 魔兽 内丹 妖核 炼丹师 炼器师 炼器 炼体 核心弟子 亲传弟子 邪修); items (玉简 符箓 法器 灵宝 仙器 神器 储物戒指 灵草 灵药); body/soul (丹田 经脉 穴位 元神 神魂 识海 肉身 分身); techniques/states (神通 法术 御剑 剑意 剑气 禁制 结界 双修 传音 心魔劫 雷劫); misc furniture (机缘 造化 悟性 资质 寿元 陨落 坐化 得道 成仙 天材地宝).

### 2. New `novel-translator/assets/catalogues/zh-wuxia.json` (~50 terms)
江湖 武林 大侠 侠客 游侠 门派 帮派 镖局 镖师 魔教 正道 邪道 名门正派 武林大会 盟主 师门 师徒 恩怨 恩情 报仇 快意恩仇 江湖规矩 义气 / 内力 内功 外功 真气 内劲 武功 轻功 招式 秘籍(+秘笈 as variant) 绝技 内功心法 剑法 刀法 拳法 掌法 点穴 任督二脉 奇经八脉 剑客 刀客 暗器 神兵 高手(with alts) 宗师 武圣 天下第一 刺客 / 少林 武当 峨眉 丐帮 / 抱拳 拱手 内伤 疗伤 解药 银两 银票 隐士.

### 3. New `novel-translator/assets/catalogues/zh-modern.json` (~60 terms) — gaming/meme/transmigration
Transmigration frame: 穿越 穿越者 重生 重生者 宿主 系统 叮 原主 剧情 女配 男配 白月光; tropes: 主角 猪脚 女主 男主 配角 路人甲 炮灰 反派 大反派 主角光环 气运 气运之子 天选之子 天才 废柴 废物 逆袭 打脸 扮猪吃老虎 装逼 低调 找死 作死 退婚 赘婿 后宫 种马 龙傲天 玛丽苏 金手指 外挂 开挂 氪金 签到; gaming: 升级 经验值 装备 副本 新手村 团灭 秒杀 刷怪 技能 属性 面板 天赋 任务 商城 背包 排行榜 抽奖 十连抽 积分 兑换; memes (stable renderings only): 咸鱼 躺平 内卷 摆烂 吃瓜 吃瓜群众 柠檬精 社死 摸鱼.

### 4. Doc update: `novel-translator/references/file-formats.md`
In the Catalogues section: add `alt_translations` to the schema example and note that multiple catalogues per language are all loaded.

## Implementation sequence

1. **Dispatch three parallel general-purpose subagents** (one per catalogue file, per your subagent-orchestration preference). Each prompt is self-contained: file path, exact JSON format with a verbatim example entry, the conventions above, the balance-matching semantics, the safety rules, and its full term bucket list. Each agent writes the file and self-checks JSON validity.
2. **Validation script** (main agent, throwaway): load all catalogues via `glossary.load_catalogue`; assert required fields, categories in enum, non-empty definitions, `variants` ≠ source, and **no duplicate source/variant across all catalogues**; simulate `count_in_text` seeding per test project (`tests/fixture-project`, `realtest-project`, `sotn-project`) and list which new terms would seed. **fixture-project must gain zero terms** — if a new term collides ≥3× with fixture text, adjust `tests/make_fixture.py` wording and regenerate.
3. **Traditional-variant audit**: verify every simplified↔traditional pair in the new entries (spot-checked by a review agent in step 5; wrong variants silently break traditional-source novels).
4. **Update `file-formats.md`** (small edit, main agent).
5. **QA per your global workflow**: `feature-dev:code-reviewer` agent + Kimi CLI + OpenCode CLI each review the three catalogues for wrong/unidiomatic translations, gate-risky entries, and bad traditional variants; fix flagged issues and re-run the validation script.
6. **Sync to installed skill**: check whether `C:\Users\chana\.zcode\skills\novel-translator` is a junction (then nothing to do) or a real copy (then copy the three catalogues + file-formats.md over).
7. Run the existing mock-server test flow to confirm no regressions, then report term counts per file/category.

**No changes to any Python code** — the loader, seeder, and pipeline already support all of this.