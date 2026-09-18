import { useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import { ArrowRight, Images, MessageSquareText, Sparkles, Wand2 } from 'lucide-react';
import { fetchMaterials, imageUrl } from '../api';
import type { MaterialsResponse } from '../types';

const capabilities = [
  {
    icon: MessageSquareText,
    title: '标题重写',
    text: '基于搜索意图与商品事实重写广告标题，保留关键卖点，控制长度与可读性。',
  },
  {
    icon: Images,
    title: '封面优化',
    text: '按角色、颜色与标签从素材库中选择最匹配查询的封面图，并给出替换理由。',
  },
  {
    icon: Wand2,
    title: '轮播生成',
    text: '把素材编排成多 slide 轮播计划，每页一个清晰的信息层级与叙事顺序。',
  },
];

const flow = [
  { title: '读取素材', text: '商品事实、SKU、价格与全部图片元数据。' },
  { title: '解析意图', text: 'Agent 理解用户指令，路由到对应 skill。' },
  { title: '执行 Skill', text: '标题重写 / 封面优化 / 轮播生成，输出结构化创意。' },
  { title: '实时预览', text: '创意同步到搜索广告预览，所见即所得。' },
];

export default function Home() {
  const [materials, setMaterials] = useState<MaterialsResponse | null>(null);

  useEffect(() => {
    fetchMaterials()
      .then(setMaterials)
      .catch(() => setMaterials(null));
  }, []);

  const images = materials?.images ?? [];
  const cover =
    images.find((img) => img.id === materials?.initial_cover_image_id) ??
    images[0];
  const thumbs = images.filter((img) => img !== cover).slice(0, 3);
  const product = materials?.product;

  return (
    <div className="page container">
      <section className="hero">
        <div>
          <p className="eyebrow">Skills-first Creative Workbench</p>
          <h1 className="hero-title">
            用可组合的 skills，把搜索广告创意改到点子上。
          </h1>
          <p className="lede">
            QueryCraft 连接商品素材库与三个专注的创意 skill：标题重写、封面优化、轮播生成。
            和 Agent 对话，创意改动立刻反映在真实的搜索广告预览里。
          </p>
          <div className="hero__actions">
            <Link className="btn" to="/playground">
              <Sparkles size={16} aria-hidden="true" /> 直接进入 Agent
            </Link>
            <Link className="btn btn--ghost" to="/skills">
              查看 3 个 Skills <ArrowRight size={16} aria-hidden="true" />
            </Link>
          </div>
          <p className="hero-note">示例素材为 AI 生成的测试素材。</p>
        </div>

        <div className="card ad-demo" aria-label="搜索广告创意示例">
          {cover ? (
            <>
              <img
                className="ad-demo__cover"
                src={imageUrl(cover.filename)}
                alt={cover.alt}
              />
              <div className="ad-demo__thumbs">
                {thumbs.map((img) => (
                  <img
                    key={img.id}
                    className="ad-demo__thumb"
                    src={imageUrl(img.filename)}
                    alt=""
                  />
                ))}
              </div>
            </>
          ) : (
            <div className="state-box">正在从素材库加载示例…</div>
          )}
          <p className="ad-demo__title">
            {product?.original_title ?? '示例广告标题将在这里展示'}
          </p>
          <ul className="ad-demo__facts">
            {(product?.facts ?? ['卖点一', '卖点二', '卖点三']).slice(0, 3).map((fact) => (
              <li key={fact}>· {fact}</li>
            ))}
          </ul>
          <div className="ad-demo__meta">
            <span>价格 {product?.price ?? '—'}</span>
            <span>SKU {product?.sku ?? '—'}</span>
          </div>
        </div>
      </section>

      <section style={{ marginTop: 40 }}>
        <h2 className="section-title">三个能力</h2>
        <div className="grid-3">
          {capabilities.map((cap) => (
            <div className="card cap-card" key={cap.title}>
              <span className="icon-chip">
                <cap.icon size={18} aria-hidden="true" />
              </span>
              <h3>{cap.title}</h3>
              <p>{cap.text}</p>
            </div>
          ))}
        </div>
      </section>

      <section style={{ marginTop: 40 }}>
        <h2 className="section-title">端到端流程</h2>
        <div className="flow">
          {flow.map((step) => (
            <div className="card flow-step" key={step.title}>
              <strong>{step.title}</strong>
              <span>{step.text}</span>
            </div>
          ))}
        </div>
      </section>
    </div>
  );
}
