import { tenantsFixture } from '../../fixtures/admin'

function fmt(n: number) { return Math.round(n / 1000) + 'k' }

export function TenantsPage() {
  return (
    <div>
      <h2 className="text-sm text-vigil-bright font-bold mb-4">Tenants</h2>
      <table className="w-full border-collapse">
        <thead>
          <tr className="text-[10px] text-vigil-muted uppercase tracking-widest border-b border-vigil-border">
            <th className="text-left py-2 px-3 font-normal">Tenant</th>
            <th className="text-left py-2 px-3 font-normal">Status</th>
            <th className="text-left py-2 px-3 font-normal">Budget Used</th>
            <th className="py-2 px-3 font-normal text-left">Edit</th>
          </tr>
        </thead>
        <tbody>
          {tenantsFixture.map(t => (
            <tr key={t.id} className={`text-xs border-b border-vigil-border ${t.status === 'suspended' ? 'opacity-50' : ''}`}>
              <td className="py-2 px-3 text-vigil-bright">{t.name}</td>
              <td className="py-2 px-3">
                <span className={t.status === 'active' ? 'text-green-500' : 'text-red-500'}>
                  ● {t.status === 'active' ? 'Active' : 'Suspended'}
                </span>
              </td>
              <td className="py-2 px-3 text-vigil-body">
                {fmt(t.budgetUsed)} / {fmt(t.budgetLimit)}
              </td>
              <td className="py-2 px-3">
                <button className="text-vigil-accent-text text-xs hover:underline">Edit</button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
