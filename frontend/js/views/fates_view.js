export const fatesViewTemplate = `
<section class="panel">
  <h2>过期入票命运册（甲路径）</h2>
  <p class="hint">校准失效地磅的过磅写口整笔回滚拒收，正式册零增；命运按当班 shift_key 锁死，偏航冲击拒写本册。</p>
  <div class="actions">
    <button class="secondary" @click="refresh">刷新</button>
  </div>
  <table class="data-table">
    <thead>
      <tr><th>当班键</th><th>地磅</th><th>车道</th><th>票号</th><th>提单</th><th>毛重</th><th>决策</th><th>路径</th><th>原因</th><th>操作人</th><th>时间</th></tr>
    </thead>
    <tbody>
      <tr v-for="f in expiredFates" :key="f.id">
        <td class="mono">{{ f.shift_key }}</td>
        <td>{{ f.bridge_code }}</td>
        <td>{{ f.lane_code }}</td>
        <td>{{ f.ticket_code }}</td>
        <td>{{ f.bl_ref }}</td>
        <td>{{ f.gross_t == null ? '—' : fmtT(f.gross_t) }}</td>
        <td><span class="badge fail">{{ f.decision }}</span></td>
        <td><span class="badge warn">甲</span></td>
        <td>{{ f.reason }}</td>
        <td>{{ f.actor }}</td>
        <td class="mono">{{ f.created_at }}</td>
      </tr>
      <tr v-if="!expiredFates.length">
        <td colspan="11" class="hint">本班无过期禁入命运</td>
      </tr>
    </tbody>
  </table>
</section>
`;
