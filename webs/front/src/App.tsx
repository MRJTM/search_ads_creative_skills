import { NavLink, Route, Routes } from 'react-router-dom';
import { Search, Sparkles, Wrench } from 'lucide-react';
import Home from './pages/Home';
import Playground from './pages/Playground';
import Skills from './pages/Skills';

function Header() {
  const linkClass = ({ isActive }: { isActive: boolean }) =>
    `nav-link${isActive ? ' nav-link--active' : ''}`;
  return (
    <header className="site-header">
      <div className="site-header__inner container">
        <NavLink to="/" className="brand">
          <span className="brand__mark" aria-hidden="true">Q</span>
          <span className="brand__name">QueryCraft</span>
          <span className="brand__tag">搜索广告创意工作台</span>
        </NavLink>
        <nav className="site-nav" aria-label="主导航">
          <NavLink to="/" end className={linkClass}>
            <Search size={15} aria-hidden="true" /> 主页
          </NavLink>
          <NavLink to="/playground" className={linkClass}>
            <Sparkles size={15} aria-hidden="true" /> Agent 体验
          </NavLink>
          <NavLink to="/skills" className={linkClass}>
            <Wrench size={15} aria-hidden="true" /> Skills
          </NavLink>
        </nav>
      </div>
    </header>
  );
}

export default function App() {
  return (
    <div className="app-shell">
      <Header />
      <main id="main">
        <Routes>
          <Route path="/" element={<Home />} />
          <Route path="/playground" element={<Playground />} />
          <Route path="/skills" element={<Skills />} />
        </Routes>
      </main>
      <footer className="site-footer">
        <div className="container">
          QueryCraft · 示例素材为 AI 生成的测试素材，仅用于演示与测试。
        </div>
      </footer>
    </div>
  );
}
