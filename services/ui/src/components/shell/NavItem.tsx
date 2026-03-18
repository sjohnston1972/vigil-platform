import { NavLink } from 'react-router-dom'

interface Props {
  to: string
  label: string
  icon: string
  end?: boolean
}

export function NavItem({ to, label, icon, end }: Props) {
  return (
    <NavLink
      to={to}
      end={end}
      className={({ isActive }) =>
        `flex items-center gap-2 px-3 py-1.5 text-sm rounded-r cursor-pointer border-l-2 transition-colors ${
          isActive
            ? 'border-vigil-accent bg-vigil-card text-vigil-bright'
            : 'border-transparent text-vigil-muted hover:text-vigil-body'
        }`
      }
    >
      <span>{icon}</span>
      <span>{label}</span>
    </NavLink>
  )
}
