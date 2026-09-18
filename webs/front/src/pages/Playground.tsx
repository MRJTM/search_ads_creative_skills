import { useEffect, useMemo, useRef, useState } from 'react';
import { AlertCircle, Layers, Send, Sparkles } from 'lucide-react';
import { fetchMaterials, sendChat } from '../api';
import AdPreview from '../components/AdPreview';
import AgentMarkdown from '../components/AgentMarkdown';
import type {
  AgentChatResponse,
  ChatMessage,
  Creative,
  MaterialsResponse,
  TraceStep,
} from '../types';

const QUICK_PROMPTS = ['优化标题', '换成蓝色封面', '生成轮播'];

const WELCOME: ChatMessage = {
  id: 'welcome',
  role: 'assistant',
  text: '你好，我是 QueryCraft Agent。你可以让我优化广告标题、换成蓝色封面，或生成一套轮播计划。右侧预览会实时更新。',
};

function makeSessionId(): string {
  return `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;
}

function toCreative(data: MaterialsResponse): Creative {
  return {
    title: data.product.original_title,
    facts: data.product.facts,
    price: data.product.price,
    sku: data.product.sku,
    cover_image_id: data.initial_cover_image_id,
    images: data.images,
    carousel: [],
  };
}

export default function Playground() {
  const [creative, setCreative] = useState<Creative | null>(null);
  const [activeImageId, setActiveImageId] = useState<number | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([WELCOME]);
  const [input, setInput] = useState('');
  const [loading, setLoading] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [trace, setTrace] = useState<TraceStep[]>([]);
  const sessionId = useMemo(makeSessionId, []);
  const listRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    fetchMaterials()
      .then((data) => {
        setCreative(toCreative(data));
        setActiveImageId(data.initial_cover_image_id);
      })
      .catch((err: unknown) =>
        setError(err instanceof Error ? err.message : '素材加载失败'),
      )
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    listRef.current?.scrollTo({ top: listRef.current.scrollHeight });
  }, [messages, sending]);

  async function handleSend(text: string) {
    const message = text.trim();
    if (!message || sending) return;
    setInput('');
    setError(null);
    setMessages((prev) => [
      ...prev,
      { id: `u-${Date.now()}`, role: 'user', text: message },
    ]);
    setSending(true);
    try {
      const data: AgentChatResponse = await sendChat(message, sessionId);
      setMessages((prev) => [
        ...prev,
        { id: `a-${Date.now()}`, role: 'assistant', text: data.reply },
      ]);
      setCreative(data.creative);
      setActiveImageId(data.creative.cover_image_id);
      setTrace(data.trace ?? []);
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Agent 请求失败，请稍后重试');
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="page container">
      <p className="eyebrow">Agent Playground</p>
      <h1 className="hero-title" style={{ fontSize: 28 }}>
        创意工作台
      </h1>
      <p className="lede">
        左侧与 Agent 对话，右侧是搜索广告的实时预览：多图在上、标题在下、标题下信息框。
      </p>

      {loading && (
        <div className="card state-box" role="status">
          正在加载素材库与初始创意…
        </div>
      )}
      {error && !loading && (
        <div className="error-box" role="alert">
          <AlertCircle size={16} aria-hidden="true" /> {error}
        </div>
      )}

      <div className="workbench">
        {/* Left: conversation */}
        <section className="card panel" aria-label="Agent 对话">
          <div className="panel__head">
            <p className="panel__title">对话</p>
            <span className="badge badge--cobalt">
              <Sparkles size={11} aria-hidden="true" /> Agent
            </span>
          </div>

          <div className="chat-list" ref={listRef} aria-live="polite">
            {messages.map((msg) => (
              <div key={msg.id} className={`chat-msg chat-msg--${msg.role}`}>
                <div className="chat-msg__role">
                  {msg.role === 'user' ? '你' : 'Agent'}
                </div>
                {msg.role === 'assistant' ? (
                  <AgentMarkdown text={msg.text} />
                ) : (
                  <div className="chat-msg__text">{msg.text}</div>
                )}
              </div>
            ))}
            {sending && (
              <div className="loading-dot" role="status">
                <span
                  className="loading-dot__dot"
                  aria-hidden="true"
                  style={{ width: 6, height: 6, borderRadius: 999, background: 'var(--cobalt)' }}
                />
                Agent 正在思考…
              </div>
            )}
          </div>

          <form
            className="chat-form"
            onSubmit={(e) => {
              e.preventDefault();
              void handleSend(input);
            }}
          >
            <input
              className="chat-input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              placeholder="例如：优化标题，突出真丝材质"
              aria-label="输入给 Agent 的指令"
            />
            <button
              type="submit"
              className="btn"
              disabled={sending || input.trim() === ''}
              aria-label="发送指令给 Agent"
            >
              <Send size={15} aria-hidden="true" /> 发送
            </button>
          </form>

          <div className="quick-prompts" aria-label="快捷指令">
            {QUICK_PROMPTS.map((prompt) => (
              <button
                key={prompt}
                type="button"
                className="chip"
                onClick={() => void handleSend(prompt)}
                disabled={sending}
                aria-label={`快捷指令：${prompt}`}
              >
                {prompt}
              </button>
            ))}
          </div>

          {trace.length > 0 && (
            <div className="trace-box">
              执行轨迹
              <ul>
                {trace.map((item, i) => (
                  <li key={`${i}-${item.step}`}>
                    <strong>{item.step}</strong>{item.detail ? ` · ${item.detail}` : ''}
                  </li>
                ))}
              </ul>
            </div>
          )}
        </section>

        {/* Right: live ad preview */}
        <section className="card panel" aria-label="搜索广告实时预览">
          <div className="panel__head">
            <p className="panel__title">搜索广告预览</p>
            <span className="badge">实时</span>
          </div>
          {creative ? (
            <>
              <AdPreview
                creative={creative}
                activeImageId={activeImageId}
                onSelectImage={setActiveImageId}
                interactive
              />
              {creative.carousel && creative.carousel.length > 0 && (
                <div className="carousel-plan">
                  <span className="badge badge--cobalt">
                    <Layers size={11} aria-hidden="true" /> 轮播计划
                  </span>
                  <ol>
                    {creative.carousel.map((slide) => (
                      <li key={`${slide.order}-${slide.image_id}`}>
                        Slide {slide.order} · {slide.source_type}：{slide.overlay_copy}
                      </li>
                    ))}
                  </ol>
                </div>
              )}
            </>
          ) : (
            !loading && (
              <div className="state-box">
                尚无创意数据。请确认后端已启动，然后刷新页面。
              </div>
            )
          )}
        </section>
      </div>
    </div>
  );
}
