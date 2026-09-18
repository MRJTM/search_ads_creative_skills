import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, Images, MessageSquareText, Wand2 } from 'lucide-react';
import { fetchSkills } from '../api';
import type { SkillSummary } from '../types';

interface SkillDetail {
  id: string;
  icon: typeof Wand2;
  name: string;
  summary: string;
  input: string;
  decisions: string;
  output: string;
  boundaries: string;
}

const DETAILS: SkillDetail[] = [
  {
    id: 'search-ads-query-title-rewrite',
    icon: MessageSquareText,
    name: '标题重写',
    summary: '按搜索查询意图重写广告标题。',
    input:
      '用户查询 / 指令、商品原始标题、商品事实（facts）、价格与 SKU。可选：用户偏好的语气或长度约束。',
    decisions:
      '从查询中提取核心意图词；将其尽量前置；从 facts 中挑选与意图最相关的 1–2 个卖点；保证标题在移动端可完整展示的长度内，且不虚构商品没有的事实。',
    output: '一条重写后的标题文本，以及简要的重写理由（哪些词被替换、为什么）。',
    boundaries:
      '不修改价格、SKU 与图片；不输出多个候选让用户盲选；查询与商品完全无关时拒绝改写并说明原因。',
  },
  {
    id: 'search-ads-query-cover-optimize',
    icon: Images,
    name: '封面优化',
    summary: '从素材库选择最匹配查询的封面图。',
    input:
      '用户查询 / 指令（如“换成蓝色封面”）、素材图片列表（含 role、colors、tags、alt）。',
    decisions:
      '若指令指定颜色或款式，先保证 query / SKU 匹配，再综合 role、tags 与视觉表现评分；查询一致性高于默认封面和通用审美。',
    output: '选中的 cover_image_id 与选择理由；未命中时保留原封面并说明。',
    boundaries:
      '优先复用现有素材；没有完全匹配时只给最小编辑建议，不改变标题与商品事实，也不制造不存在的款式。',
  },
  {
    id: 'search-ads-query-carousel-generate',
    icon: Wand2,
    name: '轮播生成',
    summary: '把素材编排成多 slide 轮播计划。',
    input: '商品事实、素材列表、当前标题与封面；可选：用户指定的叙事重点。',
    decisions:
      '先理解每张图的主体、场景、细节与完整性，再按“封面钩子 → 上身证明 → 款式/场景 → 细节收束”规划 3–5 张；逐张决定 reuse、edit 或 generate。',
    output: 'slide 列表（顺序、素材动作、image_id、编辑/生成提示词、约 10 词贴图文案与意图）。',
    boundaries:
      '复用/编辑必须引用真实 image_id，生成使用 -1；文案只引用可验证卖点，并保持图、query 与落地页一致。',
  },
];

export default function Skills() {
  const [summaries, setSummaries] = useState<SkillSummary[]>([]);

  useEffect(() => {
    fetchSkills()
      .then(setSummaries)
      .catch(() => setSummaries([]));
  }, []);

  return (
    <div className="page container">
      <p className="eyebrow">Skills</p>
      <h1 className="hero-title" style={{ fontSize: 30 }}>
        三个专注的创意 skill
      </h1>
      <p className="lede">
        每个 skill 只做一件事、边界清晰，可独立通过 CLI 运行，也可由 Agent 组合调度。
      </p>

      <div className="skills-list">
        {DETAILS.map((skill) => {
          const remote = summaries.find((s) => s.id === skill.id);
          return (
            <article className="card skill-card" key={skill.id}>
              <div>
                <span className="icon-chip">
                  <skill.icon size={18} aria-hidden="true" />
                </span>
                <h3>{remote?.name ?? skill.name}</h3>
                <p className="skill-card__id">{skill.id}</p>
                <p style={{ fontSize: 13.5, color: 'var(--ink-soft)', margin: 0 }}>
                  {remote?.description ?? skill.summary}
                </p>
              </div>
              <div className="skill-card__body">
                <h4>输入</h4>
                <p>{skill.input}</p>
                <h4>决策逻辑</h4>
                <p>{skill.decisions}</p>
                <h4>输出</h4>
                <p>{skill.output}</p>
                <h4>边界</h4>
                <p>{skill.boundaries}</p>
              </div>
            </article>
          );
        })}
      </div>

      <div className="skills-cta">
        <Link className="btn" to="/playground">
          去 Agent 体验里试试 <ArrowRight size={16} aria-hidden="true" />
        </Link>
        <span style={{ fontSize: 13, color: 'var(--ink-faint)' }}>
          试试快捷指令：优化标题 · 换成蓝色封面 · 生成轮播
        </span>
      </div>
    </div>
  );
}
