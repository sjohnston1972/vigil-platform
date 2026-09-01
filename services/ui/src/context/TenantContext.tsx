import { createContext, useContext, useEffect, useState } from 'react'
import type { ReactNode } from 'react'

interface TenantContextValue {
  tenantId: string
}

const TenantContext = createContext<TenantContextValue>({ tenantId: 'dev' })

export function TenantProvider({ children }: { children: ReactNode }) {
  const [tenantId, setTenantId] = useState('dev')

  useEffect(() => {
    fetch('/auth/me')
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (data?.tenant_id) setTenantId(data.tenant_id) })
      .catch(() => {/* local dev — use 'dev' */})
  }, [])

  return (
    <TenantContext.Provider value={{ tenantId }}>
      {children}
    </TenantContext.Provider>
  )
}

export function useTenant() {
  return useContext(TenantContext)
}
