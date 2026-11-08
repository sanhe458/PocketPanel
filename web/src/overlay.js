// 浮层栈：Modal 与 Sheet 共用，保证 Esc 只关闭最上面一层
const stack = []

export function pushOverlay(token) {
  stack.push(token)
  return token
}

export function removeOverlay(token) {
  const i = stack.indexOf(token)
  if (i >= 0) stack.splice(i, 1)
}

// 返回 true 表示当前层是最上层，应当响应 Esc
export function isTopOverlay(token) {
  return stack.length > 0 && stack[stack.length - 1] === token
}
