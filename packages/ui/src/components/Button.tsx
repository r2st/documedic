export function Button({
  children,
  className = '',
  variant = 'primary',
  size = 'default',
  type = 'button',
  ...props
}: React.ButtonHTMLAttributes<HTMLButtonElement> & {
  variant?: 'primary' | 'secondary' | 'danger' | 'ghost';
  size?: 'sm' | 'default' | 'lg';
}) {
  const baseStyles =
    'inline-flex items-center justify-center font-medium transition-all duration-150 disabled:opacity-50 disabled:cursor-not-allowed focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-offset-2 focus-visible:ring-offset-[#0A0A0B]';

  const variantStyles = {
    primary:
      'bg-[#F0B429] text-[#0A0A0B] hover:bg-[#F7D070] active:bg-[#D4A017] focus-visible:ring-[#F0B429] shadow-sm hover:shadow',
    secondary:
      'bg-[#1A1A1D] text-[#E5E7EB] border border-[#2A2A2D] hover:bg-[#222225] hover:border-[#333336] active:bg-[#2A2A2D] focus-visible:ring-[#F0B429] shadow-sm',
    danger:
      'bg-red-600 text-white hover:bg-red-700 active:bg-red-800 focus-visible:ring-red-500 shadow-sm hover:shadow',
    ghost:
      'text-[#9CA3AF] hover:text-[#E5E7EB] hover:bg-[#1A1A1D] active:bg-[#222225] focus-visible:ring-[#F0B429]',
  }[variant];

  const sizeStyles = {
    sm: 'rounded-lg px-3 py-1.5 text-xs gap-1.5',
    default: 'rounded-lg px-4 py-2.5 text-sm gap-2',
    lg: 'rounded-xl px-6 py-3 text-base gap-2.5',
  }[size];

  return (
    <button
      type={type}
      className={`${baseStyles} ${variantStyles} ${sizeStyles} ${className}`}
      {...props}
    >
      {children}
    </button>
  );
}
