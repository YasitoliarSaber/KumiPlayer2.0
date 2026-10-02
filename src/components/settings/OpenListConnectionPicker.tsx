import { Dropdown, Field, Option } from '@fluentui/react-components'
import type { OpenListConnectionConfig } from '../../api/config'
import '../../styles/openlist-connection-picker.css'

export default function OpenListConnectionPicker({ connections, value, onChange, disabled = false }: {
  connections: OpenListConnectionConfig[]
  value: string
  onChange: (connectionId: string) => void
  disabled?: boolean
}) {
  const labelFor = (connection: OpenListConnectionConfig) => `${connection.name}${connection.openlist_configured ? '' : '（未配置）'}`
  const selected = connections.find(connection => connection.connection_id === value)
  return <Field label="WebDAV 连接" className="settings-connection-field">
    <Dropdown aria-label="WebDAV 连接" value={selected ? labelFor(selected) : '选择连接'} selectedOptions={[value]} disabled={disabled} inlinePopup
      className="settings-connection-dropdown" positioning={{ position: 'below', align: 'start', offset: 4, matchTargetSize: 'width' }}
      listbox={{ className: 'settings-connection-listbox' }}
      onOptionSelect={(_, data) => { if (data.optionValue) onChange(data.optionValue) }}>
      {connections.map(connection => <Option key={connection.connection_id} value={connection.connection_id} text={labelFor(connection)}>{labelFor(connection)}</Option>)}
    </Dropdown>
  </Field>
}
