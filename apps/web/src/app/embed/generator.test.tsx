import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: { children: React.ReactNode; href: string; [k: string]: unknown }) => (
    <a href={href} {...props}>{children}</a>
  ),
}));

import { EmbedGenerator } from './generator';

describe('EmbedGenerator', () => {
  it('renders the form and embed code', () => {
    render(<EmbedGenerator />);
    expect(screen.getByText('Embed Widget Generator')).toBeInTheDocument();
    expect(screen.getByText('Embed Code')).toBeInTheDocument();
  });

  it('generates snippet with default values', () => {
    render(<EmbedGenerator />);
    const pre = document.querySelector('pre');
    expect(pre?.textContent).toContain('data-position="bottom-right"');
    expect(pre?.textContent).toContain('data-theme="dark"');
    expect(pre?.textContent).toContain('med.doaide.com/widget.js');
  });

  it('updates position in snippet', () => {
    render(<EmbedGenerator />);
    fireEvent.click(screen.getByText('bottom left'));
    const pre = document.querySelector('pre');
    expect(pre?.textContent).toContain('data-position="bottom-left"');
  });

  it('updates theme in snippet', () => {
    render(<EmbedGenerator />);
    fireEvent.click(screen.getByText('light'));
    const pre = document.querySelector('pre');
    expect(pre?.textContent).toContain('data-theme="light"');
  });

  it('copies embed code to clipboard', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<EmbedGenerator />);
    fireEvent.click(screen.getByText('Copy Code'));
    expect(writeText).toHaveBeenCalled();
    await waitFor(() => expect(screen.getByText('Copied!')).toBeInTheDocument());
  });

  it('handles copy failure gracefully', () => {
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('fail')) } });
    render(<EmbedGenerator />);
    fireEvent.click(screen.getByText('Copy Code'));
    expect(screen.getByText('Copy Code')).toBeInTheDocument();
  });

  it('updates brand color via text input', () => {
    render(<EmbedGenerator />);
    const textInput = screen.getByDisplayValue('#F0B429');
    fireEvent.change(textInput, { target: { value: '#FF0000' } });
    const pre = document.querySelector('pre');
    expect(pre?.textContent).toContain('data-color="#FF0000"');
  });
});
