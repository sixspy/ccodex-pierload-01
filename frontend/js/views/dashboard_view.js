export const dashboardViewTemplate = `
<section class="panel">
  <h2>闸口总览</h2>
  <p class="hint">过磅票入闸后裁定超重才放行，驳回进旁路挂起；闸代快照须与提单册对账。</p>
  <div class="hero-cards">
    <div class="hero-card"><span class="t">地磅桥</span><div class="n">{{ dash.bridges ?? '—' }}</div></div>
    <div class="hero-card"><span class="t">开放超重</span><div class="n">{{ dash.open_overloads ?? '—' }}</div></div>
    <div class="hero-card"><span class="t">旁路挂起</span><div class="n">{{ dash.side_pending ?? '—' }}</div></div>
    <div class="hero-card"><span class="t">过磅票</span><div class="n">{{ dash.tickets ?? '—' }}</div></div>
    <div class="hero-card"><span class="t">已放行</span><div class="n">{{ dash.gate_releases ?? '—' }}</div></div>
    <div class="hero-card"><span class="t">已封闸</span><div class="n">{{ dash.gate_seals ?? '—' }}</div></div>
    <div class="hero-card" :class="{ 'hero-alert': (dash.expired_bridges ?? 0) > 0 }">
      <span class="t">校准过期地磅</span>
      <div class="n" :class="{ 'n-bad': (dash.expired_bridges ?? 0) > 0 }">{{ dash.expired_bridges ?? '—' }}</div>
    </div>
    <div class="hero-card" :class="{ 'hero-alert': (dash.expired_fate_rejections ?? 0) > 0 }">
      <span class="t">过期禁入命运</span>
      <div class="n" :class="{ 'n-bad': (dash.expired_fate_rejections ?? 0) > 0 }">{{ dash.expired_fate_rejections ?? '—' }}</div>
    </div>
  </div>
  <p class="hint" style="margin-top:16px">骨架：{{ dash.meta && dash.meta.skeleton }}</p>
</section>
`;