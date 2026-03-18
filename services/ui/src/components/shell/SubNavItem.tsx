import { NavLink } from 'react-router-dom'

interface Props {
  to: string
  label: string
}

export function SubNavItem({ to, label }: Props) {
  return (
    <NavLink
      to={to}
      end
      className={({ isActive }) =>
        `block pl-8 pr-3 py-1 text-xs border-l-2 transition-colors ${
          isActive
            ? 'border-vigil-accent text-vigil-bright'
            : 'border-transparent text-vigil-muted hover:text-vigil-body'
        }`
      }
    >
      {label}
    </NavLink>
  )
}
