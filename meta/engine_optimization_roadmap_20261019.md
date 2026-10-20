# 引擎优化路线图 (2026-10-19, 基于 57 引擎+117 因子盘点)

## 一、现状
- 57 引擎 → 11 功能组; 因子矩阵 116 维 (动量/波动/流动性/量价微观/K线形态/gi 微观结构/a158 族/wq worldquant 族/vp 族)
- 今日实战: orchestrator + 117 因子融合 + v7 回本校验三引擎一致输出, 链路已通

## 二、维度缺口 (权威资料对照)
1. **资金流维度缺失** → Jagdeesh/Titman (1993) 动量 + Lou/Polk/Skouras (2019) 「隔夜 vs 盘中收益」证明资金结构定价;
   现有 holo 库 1001 票×100 日 moneyflow (net_in/主力 r0) 可直接提取 5 个 mf_* 因子 — 提取器已写好在跑
2. **情绪/筹码维度缺失** → Baker-Wurgler (2006) 投资者情绪指数; A 股特有涨停板效应 (Liu/Stambaugh/Yuan 2019 SHE reputational premium);
   可从 em_ztpool/hotspot_engine 的涨停池数据造 涨停连板高度/封单强度/题材温度 因子
3. **波动率结构缺失** → Engle (1982) ARCH/GARCH 族; A 股已实现波动率分解 (上行/下行半变差, Barndorff-Nielsen-Shephard 2004);
   现有 daily 数据即可算 realized_vol_skew/kurt — 零新数据成本
4. **真基本面 (PIT) 缺失** → db-fin-0 管道已有 38 年财务全史, 需做 point-in-time 对齐后并入 (Fama-French 三/五因子 + A 股特有壳价值因子)

## 三、合并去重路线 (57→44, 不动校准好的复权数据)
1. sync_*×3 + gitcode_daily + cloud_*×2 + pipeline_state → unified_sync (减 6)
2. market_data + data_warehouse + data_check → data_gateway (减 2)
3. pick_algo/predict_algo/ai_pick 旧三代 → 归档留 composite_pick (减 3)
4. rsrs + rsrs_index → 参数化合一 (减 1)
5. intraday 三件套 → intraday_suite (减 1)

## 四、多引擎协同评分升级 (已部分实现)
- 现状: orchestrator phase 加权 + 双引擎一致=high 置信
- 升级: ①置信从 2 档改连续分 (一致票数/总票数的 log-odds) ②加入 factor_matrix 融合分作为第 4 票 ③引擎历史命中率动态加权 (pick_tracker 已有数据)
- 依据: Wolpert (1992) stacked generalization — 用各引擎历史准确率学习融合权重, 替代手工加权

## 五、不做的事 (纪律)
- rev_pick 反转族已 REJECT (2026-10-16 审计), 不复活
- 不动复权校准; 不追因子数量 (116→130 即可, 多了过拟合)
- 所有新因子上线前过 ls_test_22d_correction.md 分年审计
