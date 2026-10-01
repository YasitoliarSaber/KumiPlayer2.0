import { Field, Select } from '@fluentui/react-components'
import type { OpenListConnectionConfig } from '../../api/config'

export default function OpenListConnectionPicker({ connections, value, onChange, disabled = false }: {
  connections: OpenListConnectionConfig[]
  value: string
  onChange: (connectionId: string) => void
  disabled?: boolean
}) {
  return <Field label="OpenList 连接">
    <Select aria-label="OpenList 连接" value={value} disabled={disabled} onChange={(event) => onChange(event.target.value)}>
      {connections.map((connection) => <option key={connection.connection_id} value={connection.connection_id}>
        {connection.name}{connection.openlist_configured ? '' : '（未配置）'}
      </option>)}
    </Select>
  </Field>
}
