export interface Product {
  sku: string;
  original_title: string;
  facts: string[];
  price: string;
  sizes: string[];
  colors: string[];
}

export interface MaterialImage {
  id: number;
  filename: string;
  role: string;
  colors: string[];
  tags: string[];
  alt: string;
}

export interface MaterialsResponse {
  product: Product;
  images: MaterialImage[];
  initial_cover_image_id: number;
}

export interface SkillSummary {
  id: string;
  name: string;
  description: string;
}

export interface CarouselItem {
  order: number;
  source_type: 'reuse' | 'edit' | 'generate';
  image_id: number;
  prompt: string;
  overlay_copy: string;
  intent: string;
}

export interface TraceStep {
  step: string;
  detail: string;
  data: Record<string, unknown>;
}

export interface Creative {
  title: string;
  facts: string[];
  price: string;
  sku: string;
  cover_image_id: number;
  images: MaterialImage[];
  carousel: CarouselItem[];
}

export interface AgentChatResponse {
  reply: string;
  creative: Creative;
  trace: TraceStep[];
}

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
}
