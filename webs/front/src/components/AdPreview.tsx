import { useEffect, useMemo, useRef } from 'react';
import type { KeyboardEvent as ReactKeyboardEvent, PointerEvent as ReactPointerEvent } from 'react';
import { Check, ChevronLeft, ChevronRight, Star } from 'lucide-react';
import { imageUrl } from '../api';
import type { Creative } from '../types';

interface AdPreviewProps {
  creative: Creative;
  activeImageId: number | null;
  onSelectImage: (id: number) => void;
  interactive?: boolean;
}

const SWIPE_THRESHOLD_PX = 48;

/**
 * Search ad preview: multi-image carousel on top (cover first, then the rest
 * in their original relative order), title below, then an info box with
 * price / SKU / selling points.
 */
export default function AdPreview({
  creative,
  activeImageId,
  onSelectImage,
  interactive = false,
}: AdPreviewProps) {
  const orderedImages = useMemo(() => {
    const images = creative.images;
    const cover = images.find((img) => img.id === creative.cover_image_id);
    if (!cover) return images;
    return [cover, ...images.filter((img) => img.id !== cover.id)];
  }, [creative.images, creative.cover_image_id]);

  const count = orderedImages.length;
  const currentIndex = Math.max(
    0,
    orderedImages.findIndex((img) => img.id === activeImageId),
  );
  const current = orderedImages[currentIndex] ?? null;
  const currentId = current?.id ?? null;

  const viewportRef = useRef<HTMLDivElement | null>(null);
  // Gesture transient state kept in refs to avoid high-frequency re-renders.
  const pointerStartX = useRef<number | null>(null);

  useEffect(() => {
    if (pointerStartX.current !== null && count > 0) {
      pointerStartX.current = null;
    }
  }, [count]);

  const goToIndex = (index: number) => {
    const img = orderedImages[index];
    if (img) onSelectImage(img.id);
  };

  const handleKeyDown = (event: ReactKeyboardEvent<HTMLDivElement>) => {
    if (event.key === 'ArrowLeft') {
      event.preventDefault();
      goToIndex(Math.max(0, currentIndex - 1));
    } else if (event.key === 'ArrowRight') {
      event.preventDefault();
      goToIndex(Math.min(count - 1, currentIndex + 1));
    }
  };

  const handlePointerDown = (event: ReactPointerEvent<HTMLDivElement>) => {
    // Arrow buttons live inside the swipe viewport. Do not capture their
    // pointer, otherwise the subsequent click is retargeted to the viewport
    // and the button appears focused without changing slides.
    if (event.target instanceof Element && event.target.closest('button')) {
      return;
    }
    pointerStartX.current = event.clientX;
    event.currentTarget.setPointerCapture(event.pointerId);
  };

  const handlePointerUp = (event: ReactPointerEvent<HTMLDivElement>) => {
    const startX = pointerStartX.current;
    pointerStartX.current = null;
    if (startX === null) return;
    const dx = event.clientX - startX;
    if (Math.abs(dx) < SWIPE_THRESHOLD_PX) return;
    if (dx < 0) {
      goToIndex(Math.min(count - 1, currentIndex + 1));
    } else {
      goToIndex(Math.max(0, currentIndex - 1));
    }
  };

  const handlePointerCancel = () => {
    pointerStartX.current = null;
  };

  if (!current) {
    return <div className="state-box">暂无可用素材图片</div>;
  }

  const multi = count > 1;

  return (
    <div>
      <div
        className="carousel"
        role="region"
        aria-roledescription="carousel"
        aria-label={`素材图片轮播，共 ${count} 张`}
      >
        <div
          className="carousel__viewport"
          ref={viewportRef}
          tabIndex={interactive && multi ? 0 : undefined}
          onKeyDown={interactive ? handleKeyDown : undefined}
          onPointerDown={interactive ? handlePointerDown : undefined}
          onPointerUp={interactive ? handlePointerUp : undefined}
          onPointerCancel={interactive ? handlePointerCancel : undefined}
        >
          <div
            className="carousel__track"
            style={{ transform: `translateX(-${currentIndex * 100}%)` }}
          >
            {orderedImages.map((img, index) => (
              <div
                key={img.id}
                className="carousel__slide"
                aria-hidden={index !== currentIndex}
                aria-label={`第 ${index + 1} 张，共 ${count} 张`}
                role="group"
                aria-roledescription="slide"
              >
                <div className="preview__cover-wrap">
                  <img
                    className="preview__cover"
                    src={imageUrl(img.filename)}
                    alt={img.alt}
                    draggable={false}
                  />
                  {img.id === creative.cover_image_id && (
                    <span className="badge badge--amber preview__cover-tag">
                      <Star size={11} aria-hidden="true" /> Cover
                    </span>
                  )}
                </div>
              </div>
            ))}
          </div>

          {interactive && multi && (
            <>
              <button
                type="button"
                className="carousel__arrow carousel__arrow--prev"
                aria-label="上一张图片"
                disabled={currentIndex === 0}
                onClick={() => goToIndex(currentIndex - 1)}
              >
                <ChevronLeft size={20} aria-hidden="true" />
              </button>
              <button
                type="button"
                className="carousel__arrow carousel__arrow--next"
                aria-label="下一张图片"
                disabled={currentIndex === count - 1}
                onClick={() => goToIndex(currentIndex + 1)}
              >
                <ChevronRight size={20} aria-hidden="true" />
              </button>
            </>
          )}
        </div>

        {multi && (
          <div className="carousel__footer">
            <div className="carousel__dots" role="group" aria-label="轮播页码">
              {orderedImages.map((img, index) => (
                <button
                  key={img.id}
                  type="button"
                  className={`carousel__dot${
                    index === currentIndex ? ' carousel__dot--active' : ''
                  }`}
                  aria-label={`切换到第 ${index + 1} 张图片`}
                  aria-current={index === currentIndex}
                  onClick={() => goToIndex(index)}
                />
              ))}
            </div>
            <span className="carousel__count">
              {currentIndex + 1} / {count}
            </span>
          </div>
        )}
      </div>

      <div className="preview__thumbs" role="group" aria-label="素材缩略图，点击切换主预览">
        {orderedImages.map((img) => (
          <button
            key={img.id}
            type="button"
            className={`thumb-btn${
              img.id === currentId ? ' thumb-btn--active' : ''
            }`}
            aria-label={`切换预览图：${img.alt}`}
            aria-pressed={img.id === currentId}
            onClick={() => onSelectImage(img.id)}
            disabled={!interactive}
          >
            <img src={imageUrl(img.filename)} alt="" draggable={false} />
            {img.id === creative.cover_image_id && (
              <span className="badge badge--amber">
                <Star size={10} aria-hidden="true" /> Cover
              </span>
            )}
          </button>
        ))}
      </div>

      <p className="preview__title">{creative.title}</p>

      <div className="info-box">
        <ul>
          {creative.facts.map((fact) => (
            <li key={fact}>
              <Check size={13} aria-hidden="true" style={{ flexShrink: 0 }} />
              <span>{fact}</span>
            </li>
          ))}
        </ul>
        <div className="info-box__meta">
          <span>
            价格 <strong>{creative.price}</strong>
          </span>
          <span>
            SKU <strong>{creative.sku}</strong>
          </span>
        </div>
      </div>
    </div>
  );
}
