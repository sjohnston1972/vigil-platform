import { useLocation } from 'react-router-dom'
import { NavItem } from './NavItem'
import { SubNavItem } from './SubNavItem'

interface Props {
  tenantName: string
  tokensToday: number
}

export function Sidebar({ tenantName, tokensToday }: Props) {
  const { pathname } = useLocation()
  const adminActive = pathname.startsWith('/admin')

  return (
    <aside className="w-52 shrink-0 bg-vigil-sidebar border-r border-vigil-border flex flex-col h-full">
      <div className="px-4 py-4 text-vigil-accent-text font-bold tracking-widest text-sm">
        VIGIL
      </div>

      <div className="border-t border-vigil-border mb-2" />

      <nav className="flex-1 flex flex-col gap-0.5 px-1">
        <NavItem to="/dashboard" label="Dashboard" icon="⊞" end />
        <NavItem to="/chat"      label="Chat"       icon="💬" />
        <NavItem to="/audit"     label="Audit Logs" icon="📋" />
        <NavItem to="/admin"     label="Admin"      icon="⚙️" />
        {adminActive && (
          <>
            <SubNavItem to="/admin/tenants" label="Tenants" />
            <SubNavItem to="/admin/budgets" label="Token Budgets" />
            <SubNavItem to="/admin/agents"  label="Agent Health" />
          </>
        )}
      </nav>

      <div className="border-t border-vigil-border px-4 py-3 mt-auto">
        <p className="text-xs text-vigil-bright">{tenantName}</p>
        <p className="text-xs text-vigil-muted mt-0.5">
          {tokensToday.toLocaleString()} tokens today
        </p>
      </div>
    </aside>
  )
}
