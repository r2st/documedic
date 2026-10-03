import { render, screen, fireEvent, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ShareButtons } from './ShareButtons';

describe('ShareButtons', () => {
  it('renders all three share options', () => {
    render(<ShareButtons url="https://example.com" title="Test" />);
    expect(screen.getByText('WhatsApp')).toBeInTheDocument();
    expect(screen.getByText('X / Twitter')).toBeInTheDocument();
    expect(screen.getByText('Copy link')).toBeInTheDocument();
    expect(screen.getByText('Share:')).toBeInTheDocument();
  });

  it('WhatsApp link encodes title and url', () => {
    render(<ShareButtons url="https://example.com/path" title="My Title" />);
    const link = screen.getByText('WhatsApp').closest('a');
    expect(link?.getAttribute('href')).toContain('wa.me');
    expect(link?.getAttribute('href')).toContain('My%20Title');
    expect(link?.getAttribute('href')).toContain(encodeURIComponent('https://example.com/path'));
    expect(link?.getAttribute('target')).toBe('_blank');
    expect(link?.getAttribute('rel')).toBe('noopener noreferrer');
  });

  it('Twitter link encodes title and url', () => {
    render(<ShareButtons url="https://example.com" title="My Title" />);
    const link = screen.getByText('X / Twitter').closest('a');
    expect(link?.getAttribute('href')).toContain('twitter.com/intent/tweet');
    expect(link?.getAttribute('href')).toContain('My%20Title');
  });

  it('copies url to clipboard and shows confirmation', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined);
    Object.assign(navigator, { clipboard: { writeText } });
    render(<ShareButtons url="https://example.com" title="Test" />);
    fireEvent.click(screen.getByText('Copy link'));
    expect(writeText).toHaveBeenCalledWith('https://example.com');
    await waitFor(() => expect(screen.getByText('Copied!')).toBeInTheDocument());
  });

  it('handles clipboard failure gracefully', async () => {
    Object.assign(navigator, { clipboard: { writeText: vi.fn().mockRejectedValue(new Error('fail')) } });
    render(<ShareButtons url="https://example.com" title="Test" />);
    fireEvent.click(screen.getByText('Copy link'));
    expect(screen.getByText('Copy link')).toBeInTheDocument();
  });
});
